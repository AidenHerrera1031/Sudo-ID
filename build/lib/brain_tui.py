import curses
import getpass
import os
import queue
import re
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class StepState:
    status: str = "todo"
    detail: str = ""


class BrainTUI:
    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.selected = 0
        self.logs = []
        self.max_logs = 500
        self.width = 80
        self.height = 24
        self.project_root = Path(".").resolve()
        self.spinner_frames = ["|", "/", "-", "\\"]
        self.runtime = None
        self.watch_process = None
        self.watch_log_path = Path("/tmp/brain_watch_tui.log")
        self.states = {
            "recommended": StepState(),
            "init": StepState(),
            "doctor": StepState(),
            "key": StepState(),
            "sync": StepState(),
            "ask": StepState(),
            "watch": StepState(),
        }
        self.action_defs = [
            ("recommended", "Run Recommended Setup (init -> doctor -> sync)"),
            ("init", "Initialize Files (brain init)"),
            ("doctor", "Run Health Checks (brain doctor)"),
            ("key", "Set OPENAI_API_KEY"),
            ("sync", "Sync Project Memory (brain sync)"),
            ("ask", "Ask a Question (brain ask)"),
            ("watch", "Start Watch Mode (brain watch)"),
            ("stop_watch", "Stop Watch Mode"),
            ("toggle_setup", "Hide completed/setup steps"),
            ("exit", "Exit"),
        ]
        self.show_setup_steps = True
        self.onboarding_complete = False

    def append_log(self, line: str) -> None:
        line = str(line or "").rstrip("\n")
        if not line:
            return
        wrapped = textwrap.wrap(line, width=max(20, self.width - 4)) or [line]
        for part in wrapped:
            self.logs.append(part)
        if len(self.logs) > self.max_logs:
            self.logs = self.logs[-self.max_logs :]

    def _status_label(self, key: str) -> str:
        state = self.states.get(key)
        if not state:
            return ""
        status = state.status
        if status == "done":
            return "[DONE]"
        if status == "fail":
            return "[FAIL]"
        if status == "running":
            spin = self.spinner_frames[0]
            if self.runtime:
                spin = self.spinner_frames[self.runtime.get("spinner_index", 0) % len(self.spinner_frames)]
            return f"[{spin}...]"
        return "[TODO]"

    def _progress_bar(self, current: int, total: int, width: int = 24) -> str:
        if total <= 0:
            return "[" + ("." * width) + "]"
        ratio = min(1.0, max(0.0, current / total))
        filled = int(round(width * ratio))
        return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"

    def _runtime_line(self) -> str:
        if not self.runtime:
            if self._is_watch_running():
                pid = self.watch_process.pid if self.watch_process else "?"
                return f"Watch running in background (pid {pid}) | log: {self.watch_log_path}"
            return "Ready."

        name = self.runtime.get("name", "command")
        spinner = self.spinner_frames[self.runtime.get("spinner_index", 0) % len(self.spinner_frames)]
        elapsed = int(time.time() - self.runtime.get("started_at", time.time()))
        current = int(self.runtime.get("progress_current", 0) or 0)
        total = int(self.runtime.get("progress_total", 0) or 0)
        detail = str(self.runtime.get("detail", "")).strip()
        bar = self._progress_bar(current, total, width=22)
        if total > 0:
            progress_text = f"{current}/{total} ({int((current / total) * 100):3d}%)"
        else:
            progress_text = "running"
        if detail:
            detail = f" | {detail}"
        return f"Running {name}: {spinner} {bar} {progress_text}{detail} | {elapsed}s | x: cancel"

    def _is_setup_action(self, key: str) -> bool:
        return key in {"recommended", "init", "doctor", "key", "sync"}

    def _is_onboarding_complete(self) -> bool:
        recommended_done = self.states["recommended"].status == "done"
        manual_done = all(self.states[name].status == "done" for name in ("init", "doctor", "sync"))
        return recommended_done or manual_done

    def _sync_menu_mode(self) -> None:
        complete = self._is_onboarding_complete()
        if complete and not self.onboarding_complete:
            self.onboarding_complete = True
            self.show_setup_steps = False
            self.append_log("Onboarding complete. Switched to daily mode menu.")
        elif not complete:
            self.onboarding_complete = False

    def _action_label(self, key: str, label: str) -> str:
        if key == "toggle_setup":
            return "Hide completed/setup steps" if self.show_setup_steps else "Show completed/setup steps"
        if key == "recommended" and self.states["recommended"].status == "fail":
            return "Run Setup Again (last run failed)"
        if key == "watch" and self._is_watch_running():
            return "Watch Mode Running (background)"
        return label

    def _should_show_action(self, key: str) -> bool:
        if key in {"ask", "watch", "toggle_setup", "exit"}:
            return True
        if key == "stop_watch":
            return self._is_watch_running()

        state = self.states.get(key)
        if state and state.status == "running":
            return True

        if self.show_setup_steps or not self.onboarding_complete:
            return True

        if key == "recommended":
            return self.states["recommended"].status != "done"
        if key == "key":
            return not self._has_openai_key()
        if key in {"init", "doctor", "sync"}:
            return self.states[key].status != "done"
        return True

    def _visible_actions(self) -> list[tuple[str, str]]:
        self._sync_menu_mode()
        actions = []
        for key, label in self.action_defs:
            if self._should_show_action(key):
                actions.append((key, self._action_label(key, label)))
        if self.selected >= len(actions):
            self.selected = max(0, len(actions) - 1)
        return actions

    def draw(self) -> None:
        self.stdscr.erase()
        self.height, self.width = self.stdscr.getmaxyx()
        min_height = 21
        min_width = 72

        if self.height < min_height or self.width < min_width:
            msg = f"Terminal too small ({self.width}x{self.height}). Resize to at least {min_width}x{min_height}."
            self.stdscr.addnstr(0, 0, msg, self.width - 1)
            self.stdscr.refresh()
            return

        title = "Brain Setup TUI"
        subtitle = f"Project: {self.project_root}"
        help_line = "Arrows: move | Enter: run | q: quit"
        if self.runtime:
            help_line = "Running command | x: cancel | q: quit after command returns"
        runtime_line = self._runtime_line()

        self.stdscr.addnstr(0, 2, title, self.width - 4, curses.A_BOLD)
        self.stdscr.addnstr(1, 2, subtitle, self.width - 4)
        self.stdscr.addnstr(2, 2, help_line, self.width - 4)
        self.stdscr.addnstr(3, 2, runtime_line, self.width - 4)

        actions_top = 5
        actions = self._visible_actions()
        for idx, (key, label) in enumerate(actions):
            y = actions_top + idx
            attr = curses.A_REVERSE if idx == self.selected else curses.A_NORMAL
            if self._is_setup_action(key):
                line = f"{self._status_label(key)} {label}"
            else:
                line = f"    {label}"
            self.stdscr.addnstr(y, 2, line, self.width - 4, attr)

        logs_top = actions_top + len(actions) + 1
        self.stdscr.hline(logs_top, 1, curses.ACS_HLINE, self.width - 2)
        self.stdscr.addnstr(logs_top, 3, " Output ", self.width - 6, curses.A_BOLD)

        log_height = self.height - logs_top - 2
        visible = self.logs[-max(1, log_height) :]
        for i, line in enumerate(visible):
            self.stdscr.addnstr(logs_top + 1 + i, 2, line, self.width - 4)

        self.stdscr.refresh()

    def _brain_cmd(self, args) -> list[str]:
        cli_script = Path(__file__).with_name("brain_cli.py")
        return [sys.executable, str(cli_script)] + list(args)

    def _run_command(self, args, key: str) -> bool:
        self.states[key].status = "running"
        self.states[key].detail = ""
        self.runtime = {
            "name": " ".join(args),
            "started_at": time.time(),
            "spinner_index": 0,
            "progress_current": 0,
            "progress_total": 0,
            "detail": "",
        }
        self.draw()

        cmd = self._brain_cmd(args)
        self.append_log(f"$ {' '.join(args)}")
        self.draw()

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        stream_queue = queue.Queue()
        stream_done = False

        def _reader():
            if process.stdout:
                for line in process.stdout:
                    stream_queue.put(line)
            stream_queue.put(None)

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        self.stdscr.timeout(120)
        try:
            while True:
                self.runtime["spinner_index"] = self.runtime.get("spinner_index", 0) + 1

                while True:
                    try:
                        item = stream_queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is None:
                        stream_done = True
                        break
                    self._handle_runtime_output(item)

                self.draw()
                ch = self.stdscr.getch()
                if ch in {ord("x"), ord("X"), 3} and process.poll() is None:
                    self.append_log("Cancel requested...")
                    process.terminate()

                if process.poll() is not None and stream_done:
                    break
        finally:
            self.stdscr.timeout(-1)

        return_code = process.wait()
        ok = return_code == 0
        self.states[key].status = "done" if ok else "fail"
        self.states[key].detail = f"exit code {return_code}"
        self.append_log(f"-> {'ok' if ok else 'failed'} ({return_code})")
        self.runtime = None
        self.draw()
        return ok

    def _is_watch_running(self) -> bool:
        process = self.watch_process
        if process is None:
            return False
        return_code = process.poll()
        if return_code is None:
            return True
        self.watch_process = None
        ok = return_code == 0
        self.states["watch"].status = "done" if ok else "fail"
        self.states["watch"].detail = f"exit code {return_code}"
        self.append_log(f"Background watch exited ({return_code}).")
        return False

    def _handle_runtime_output(self, raw_line: str) -> None:
        line = str(raw_line or "").rstrip("\n")
        if not line:
            return

        match = re.match(r"^Sync progress:\s+(\d+)\s*/\s*(\d+)\s+\((\d+)%\)\s*(.*)$", line)
        if match and self.runtime:
            self.runtime["progress_current"] = int(match.group(1))
            self.runtime["progress_total"] = int(match.group(2))
            source = match.group(4).strip()
            if source:
                self.runtime["detail"] = source
            return

        if line.startswith("[sync] ") and self.runtime:
            self.runtime["detail"] = line.replace("[sync] ", "", 1).strip()

        self.append_log(line)

    def _suspend_for_input(self):
        curses.def_prog_mode()
        curses.endwin()

    def _resume_after_input(self):
        curses.reset_prog_mode()
        self.stdscr.refresh()
        try:
            curses.curs_set(0)
        except curses.error:
            pass

    def _ask_text(self, prompt: str, secret: bool = False) -> str:
        self._suspend_for_input()
        try:
            if secret:
                value = getpass.getpass(prompt)
            else:
                value = input(prompt)
            return (value or "").strip()
        finally:
            self._resume_after_input()

    def _confirm(self, prompt: str, default: bool = True) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        raw = self._ask_text(f"{prompt} {suffix} ")
        if not raw:
            return default
        raw = raw.lower()
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        return default

    def _has_openai_key(self) -> bool:
        if os.getenv("OPENAI_API_KEY", "").strip():
            return True
        env_path = Path(".env")
        if not env_path.exists():
            return False
        try:
            for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("OPENAI_API_KEY=") and line.split("=", 1)[1].strip():
                    return True
        except OSError:
            return False
        return False

    def _save_openai_key(self, value: str) -> None:
        env_path = Path(".env")
        lines = []
        if env_path.exists():
            try:
                lines = env_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                lines = []
        lines = [line for line in lines if not line.startswith("OPENAI_API_KEY=")]
        lines.append(f"OPENAI_API_KEY={value}")
        env_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass

    def run_recommended(self):
        self.states["recommended"].status = "running"
        init_ok = self._run_command(["init"], key="init")
        doctor_ok = self._run_command(["doctor"], key="doctor")

        if not doctor_ok:
            continue_anyway = self._confirm("Doctor reported issues. Continue with sync anyway?", default=True)
            if not continue_anyway:
                self.states["recommended"].status = "fail"
                self.states["recommended"].detail = "stopped after doctor"
                return

        sync_ok = self._run_command(["sync"], key="sync")
        self.states["recommended"].status = "done" if (init_ok and doctor_ok and sync_ok) else "fail"
        self.states["recommended"].detail = "completed" if sync_ok else "sync failed"
        if self.states["recommended"].status == "done":
            if self._confirm("Recommended setup complete. Start watch mode now?", default=True):
                self.start_watch()
            else:
                self.append_log("Skipped watch mode start.")

    def set_key(self):
        if self._has_openai_key() and not self._confirm("OPENAI_API_KEY already exists. Overwrite?", default=False):
            self.append_log("Skipped OPENAI_API_KEY update.")
            self.states["key"].status = "done"
            return

        key_value = self._ask_text("Paste OPENAI_API_KEY (blank cancels): ", secret=True)
        if not key_value:
            self.append_log("No key entered.")
            self.states["key"].status = "fail"
            return

        self._save_openai_key(key_value)
        self.append_log("OPENAI_API_KEY saved to .env")
        self.states["key"].status = "done"

    def ask_question(self):
        question = self._ask_text("Question: ")
        if not question:
            self.append_log("No question entered.")
            self.states["ask"].status = "fail"
            return
        ok = self._run_command(["ask", question], key="ask")
        self.states["ask"].status = "done" if ok else "fail"

    def start_watch(self):
        if self._is_watch_running():
            pid = self.watch_process.pid if self.watch_process else "?"
            self.append_log(f"Watch mode is already running in background (pid {pid}).")
            return

        cmd = self._brain_cmd(["watch"])
        self.append_log("Starting watch mode in background.")
        log_handle = None
        try:
            log_handle = self.watch_log_path.open("a", encoding="utf-8")
            process = subprocess.Popen(
                cmd,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            self.states["watch"].status = "fail"
            self.states["watch"].detail = str(exc)
            self.append_log(f"Failed to start watch mode: {exc}")
            return
        finally:
            if log_handle:
                log_handle.close()

        self.watch_process = process
        self.states["watch"].status = "running"
        self.states["watch"].detail = f"pid {process.pid}"
        self.append_log(
            f"Watch mode started (pid {process.pid}). You can ask questions now. Logs: {self.watch_log_path}"
        )

    def stop_watch(self):
        process = self.watch_process
        if process is None:
            self.states["watch"].status = "done"
            self.states["watch"].detail = "not running"
            self.append_log("Watch mode is not running.")
            return

        if process.poll() is not None:
            self.watch_process = None
            self.states["watch"].status = "done"
            self.states["watch"].detail = f"exit code {process.returncode}"
            self.append_log(f"Watch mode already exited ({process.returncode}).")
            return

        self.append_log("Stopping watch mode...")
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        self.watch_process = None
        self.states["watch"].status = "done"
        self.states["watch"].detail = "stopped by user"
        self.append_log("Watch mode stopped.")

    def on_enter(self) -> bool:
        actions = self._visible_actions()
        key, _label = actions[self.selected]

        if key == "exit":
            return False
        if key == "toggle_setup":
            self.show_setup_steps = not self.show_setup_steps
            self.append_log(
                "Showing completed/setup steps." if self.show_setup_steps else "Hiding completed/setup steps."
            )
            return True
        if key == "recommended":
            self.run_recommended()
            return True
        if key == "init":
            self._run_command(["init"], key="init")
            return True
        if key == "doctor":
            self._run_command(["doctor"], key="doctor")
            return True
        if key == "key":
            self.set_key()
            return True
        if key == "sync":
            self._run_command(["sync"], key="sync")
            return True
        if key == "ask":
            self.ask_question()
            return True
        if key == "watch":
            self.start_watch()
            return True
        if key == "stop_watch":
            self.stop_watch()
            return True
        return True

    def run(self) -> int:
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        try:
            curses.curs_set(0)
        except curses.error:
            pass

        while True:
            self.draw()
            ch = self.stdscr.getch()
            actions = self._visible_actions()
            if ch in {ord("q"), 27}:
                if self._is_watch_running():
                    self.stop_watch()
                return 0
            if ch in {curses.KEY_UP, ord("k")}:
                self.selected = (self.selected - 1) % len(actions)
                continue
            if ch in {curses.KEY_DOWN, ord("j")}:
                self.selected = (self.selected + 1) % len(actions)
                continue
            if ch in {10, 13, curses.KEY_ENTER}:
                keep_running = self.on_enter()
                if not keep_running:
                    return 0
                continue


def run_tui() -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("brain tui requires an interactive terminal.", file=sys.stderr)
        return 2

    def _wrapped(stdscr):
        app = BrainTUI(stdscr)
        return app.run()

    return int(curses.wrapper(_wrapped) or 0)
