import curses
import json
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

from brain_version import get_brain_version

WATCHER_SMOKE_MARKER = "glacier-lantern-4821"
WATCHER_SMOKE_QUERY = "glacier lantern 4821"


@dataclass
class StepState:
    status: str = "todo"
    detail: str = ""


class BrainTUI:
    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.selected = 0
        self.logs = []
        self.output_override_lines = []
        self.output_scroll = 0
        self.max_logs = 500
        self.width = 80
        self.height = 24
        self.project_root = Path(".").resolve()
        self.brain_version, self.version_source = get_brain_version()
        self.spinner_frames = ["|", "/", "-", "\\"]
        self.runtime = None
        self.watch_process = None
        self.watch_log_path = Path("/tmp/brain_watch_tui.log")
        self.watch_status_path = self.project_root / ".codex_brain" / "watch_status.json"
        self.last_sync_at = self._detect_last_sync_time()
        self.last_question = ""
        self.last_answer_lines = []
        self.last_answer_sections = {}
        self.project_summary = self._load_project_summary()
        self.ask_scope = "mixed"
        self.states = {
            "recommended": StepState(),
            "init": StepState(),
            "doctor": StepState(),
            "key": StepState(),
            "sync": StepState(),
            "ask": StepState(),
            "workflow": StepState(),
            "watch": StepState(),
        }
        self.action_defs = [
            ("recommended", "Run Recommended Setup"),
            ("init", "Initialize Files"),
            ("doctor", "Run Health Checks"),
            ("key", "Set OPENAI_API_KEY"),
            ("sync", "Sync Memory"),
            ("scope", "Ask Scope"),
            ("ask", "Ask a Question"),
            ("guide", "Repo Walkthrough"),
            ("map", "Find Code For Task"),
            ("changes", "Summarize Current Work"),
            ("release", "Release Readiness"),
            ("smoke_test", "Watcher Smoke Test"),
            ("watch", "Start Watch Mode"),
            ("stop_watch", "Stop Watch Mode"),
            ("toggle_setup", "Hide setup steps"),
            ("exit", "Exit"),
        ]
        self.show_setup_steps = True
        self.onboarding_complete = False
        self._bootstrap_existing_state()

    def append_log(self, line: str) -> None:
        line = str(line or "").rstrip("\n")
        if not line:
            return
        wrapped = textwrap.wrap(line, width=max(20, self.width - 4)) or [line]
        for part in wrapped:
            self.logs.append(part)
        if len(self.logs) > self.max_logs:
            self.logs = self.logs[-self.max_logs :]

    def _clear_output(self) -> None:
        self.logs = []
        self.output_override_lines = []
        self.output_scroll = 0

    def _set_output_lines(self, lines: list[str]) -> None:
        self.logs = []
        self.output_override_lines = []
        self.output_scroll = 0
        for raw in lines:
            line = str(raw or "").rstrip("\n")
            if not line:
                self.output_override_lines.append("")
                continue
            wrapped = textwrap.wrap(line, width=max(20, self.width - 4)) or [line]
            self.output_override_lines.extend(wrapped)

    def _output_lines(self) -> list[str]:
        if self.runtime:
            return self.logs
        return self.output_override_lines or self.logs

    def _max_output_scroll(self, log_height: int) -> int:
        lines = self._output_lines()
        return max(0, len(lines) - max(1, log_height))

    def _clamp_output_scroll(self, log_height: int) -> None:
        self.output_scroll = max(0, min(self.output_scroll, self._max_output_scroll(log_height)))

    def _visible_output_lines(self, log_height: int) -> list[str]:
        lines = self._output_lines()
        height = max(1, log_height)
        if self.runtime or not self.output_override_lines:
            return lines[-height:]
        self._clamp_output_scroll(height)
        start = self.output_scroll
        end = start + height
        return lines[start:end]

    def _output_status_label(self, log_height: int) -> str:
        lines = self._output_lines()
        if not lines:
            return " Output "

        height = max(1, log_height)
        if self.runtime or not self.output_override_lines:
            total = len(lines)
            start = max(1, total - height + 1)
            end = total
            return f" Output {start}-{end}/{total} "

        self._clamp_output_scroll(height)
        total = len(lines)
        start = self.output_scroll + 1
        end = min(total, self.output_scroll + height)
        return f" Output {start}-{end}/{total} "

    def _output_log_height(self, actions: list[tuple[str, str]]) -> int:
        _dashboard_lines, actions_top, logs_top = self._layout_metrics(actions)
        return max(1, self.height - logs_top - 2)

    def _layout_metrics(self, actions: list[tuple[str, str]]) -> tuple[list[str], int, int]:
        dashboard_lines = self._dashboard_lines() if self.onboarding_complete else []
        actions_top = 5
        if self.onboarding_complete:
            actions_top = 5 + len(dashboard_lines) + 2
        logs_top = actions_top + len(actions) + 1
        return dashboard_lines, actions_top, logs_top

    def _show_answer_output(self) -> None:
        lines = []
        section_order = ["Answer", "Key Points", "Files", "Missing Context", "Confidence"]
        list_sections = {"Key Points", "Files"}

        if self.last_answer_sections:
            for heading in section_order:
                values = [str(value or "").strip() for value in self.last_answer_sections.get(heading, []) if str(value or "").strip()]
                if not values:
                    continue
                lines.append(f"{heading}:")
                for value in values:
                    cleaned = value[2:].strip() if value.startswith("- ") else value
                    if heading in list_sections:
                        lines.append(f"- {cleaned}")
                    else:
                        lines.append(cleaned)
                lines.append("")
        elif self.last_answer_lines:
            lines.extend(self.last_answer_lines)

        if lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            lines = ["No answer yet."]

        self._set_output_lines(lines)

    def _show_watch_output(self) -> None:
        self._set_output_lines(
            [
                "Watch mode started.",
                "Changes will sync in the background.",
                "Use 'Stop Watch Mode' to stop it.",
            ]
        )

    def _wrap_label_value(self, label: str, value: str, width: int, max_lines: int | None = None) -> list[str]:
        prefix = f"{label}: "
        cleaned = str(value or "").strip() or "none"
        wrapper = textwrap.TextWrapper(
            width=max(20, width),
            initial_indent=prefix,
            subsequent_indent=" " * len(prefix),
            break_long_words=False,
            break_on_hyphens=False,
        )
        wrapped = wrapper.wrap(cleaned) or [prefix.rstrip()]
        if max_lines is not None and max_lines > 0 and len(wrapped) > max_lines:
            wrapped = wrapped[:max_lines]
            tail = wrapped[-1]
            if len(tail) >= max(4, width - 3):
                tail = tail[: max(0, width - 4)].rstrip()
            if not tail.endswith("..."):
                tail = tail.rstrip(".") + "..."
            wrapped[-1] = tail
        return wrapped

    def _clean_section_value(self, value: str) -> str:
        cleaned = str(value or "").strip()
        if cleaned.startswith("- "):
            return cleaned[2:].strip()
        return cleaned

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
                return "Watch running in background."
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

    def _starter_files_present(self) -> bool:
        return all(
            (self.project_root / name).exists()
            for name in (".env", ".brainignore", "brain.toml")
        )

    def _env_file_present(self) -> bool:
        return (self.project_root / ".env").exists()

    def _bootstrap_existing_state(self) -> None:
        if self._starter_files_present():
            self.states["init"].status = "done"
        if self._has_openai_key():
            self.states["key"].status = "done"
        if self.last_sync_at > 0:
            self.states["sync"].status = "done"
        if self._starter_files_present() and self.last_sync_at > 0:
            self.states["doctor"].status = "done"

    def _is_onboarding_complete(self) -> bool:
        recommended_done = self.states["recommended"].status == "done"
        manual_done = all(self.states[name].status == "done" for name in ("init", "doctor", "sync"))
        inferred_done = self._starter_files_present() and self.last_sync_at > 0
        return recommended_done or manual_done or inferred_done

    def _sync_menu_mode(self) -> None:
        complete = self._is_onboarding_complete()
        if complete and not self.onboarding_complete:
            self.onboarding_complete = True
            self.show_setup_steps = False
            self._clear_output()
        elif not complete:
            self.onboarding_complete = False

    def _action_label(self, key: str, label: str) -> str:
        if key == "toggle_setup":
            return "Hide setup steps" if self.show_setup_steps else "Show setup steps"
        if key == "recommended" and self.states["recommended"].status == "fail":
            return "Run Setup Again (last run failed)"
        if key == "scope":
            return f"Ask Scope: {self.ask_scope.title()}"
        return label

    def _should_show_action(self, key: str) -> bool:
        if key in {"ask", "scope", "watch", "toggle_setup", "exit"}:
            if key == "watch":
                return not self._is_watch_running()
            return True
        if key in {"guide", "map", "changes", "release"}:
            return self.onboarding_complete
        if key == "stop_watch":
            return self._is_watch_running()

        state = self.states.get(key)
        if state and state.status == "running":
            return True

        if key == "key":
            return not self._env_file_present()

        if self.show_setup_steps or not self.onboarding_complete:
            return True

        if key == "recommended":
            return self.states["recommended"].status != "done"
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

    def _detect_last_sync_time(self) -> float:
        candidates = [
            self.project_root / ".codex_brain" / "index_state.json",
            self.project_root / ".codex_brain" / "chroma.sqlite3",
        ]
        for path in candidates:
            try:
                if path.exists():
                    return path.stat().st_mtime
            except OSError:
                continue
        return 0.0

    def _load_project_summary(self) -> str:
        readme_path = self.project_root / "README.md"
        try:
            lines = readme_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            lines = []

        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                return stripped
        return "Terminal-first local project memory assistant."

    def _relative_time(self, ts: float) -> str:
        if ts <= 0:
            return "never"
        delta = max(0, int(time.time() - ts))
        if delta < 10:
            return "just now"
        if delta < 60:
            return f"{delta}s ago"
        if delta < 3600:
            return f"{delta // 60}m ago"
        if delta < 86400:
            return f"{delta // 3600}h ago"
        return f"{delta // 86400}d ago"

    def _memory_status_text(self) -> str:
        brain_dir = self.project_root / ".codex_brain"
        if not brain_dir.exists():
            return "not built"
        if self.states["sync"].status == "running":
            return "syncing now"
        if self.last_sync_at > 0:
            return "ready"
        return "present"

    def _load_watch_status(self) -> dict:
        try:
            if self.watch_status_path.exists():
                data = json.loads(self.watch_status_path.read_text(encoding="utf-8", errors="ignore"))
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def _parse_answer_sections(self, lines: list[str]) -> dict[str, list[str]]:
        sections = {}
        current = ""
        for raw in lines:
            line = str(raw or "").strip()
            if not line:
                continue
            if line.endswith(":") and line[:-1] in {"Answer", "Key Points", "Files", "Missing Context", "Confidence"}:
                current = line[:-1]
                sections.setdefault(current, [])
                continue
            if current:
                sections.setdefault(current, []).append(line)
        return sections

    def _extract_raw_result_block(self, lines: list[str], source_name: str) -> list[str]:
        out = []
        capturing = False
        target = f"source={source_name}"
        for raw in lines:
            line = str(raw or "").rstrip("\n")
            stripped = line.strip()
            if stripped.startswith("[") and "source=" in stripped:
                if capturing:
                    break
                if target in stripped:
                    capturing = True
            if capturing:
                out.append(line)
        return out

    def _watch_dashboard_line(self) -> str:
        status = self._load_watch_status()
        state = str(status.get("state", "")).strip().lower()
        finished = float(status.get("last_sync_finished_at", 0) or 0)
        if finished > self.last_sync_at:
            self.last_sync_at = finished

        if state == "syncing":
            return "Watch: syncing changes now"
        if state == "debouncing":
            return "Watch: changes detected, waiting to sync"
        if state == "error":
            detail = str(status.get("last_error", "")).strip() or "last sync failed"
            return f"Watch: error | {detail}"
        if state == "running":
            if finished > 0:
                ok = status.get("last_sync_ok")
                sync_label = "ok" if ok else "failed"
                return f"Watch: running | last sync {sync_label} {self._relative_time(finished)}"
            return "Watch: running"
        if self._is_watch_running():
            return "Watch: running"
        return "Watch: stopped"

    def _watch_insight_line(self) -> str:
        status = self._load_watch_status()
        subsystems = [str(item).strip() for item in status.get("subsystems", []) if str(item).strip()]
        stale_docs = [str(item).strip() for item in status.get("stale_docs", []) if str(item).strip()]
        if not subsystems and not stale_docs:
            return "Insights: none yet"

        parts = []
        if subsystems:
            parts.append("areas " + ", ".join(subsystems[:3]))
        if stale_docs:
            parts.append("docs " + ", ".join(stale_docs[:2]))
        return "Insights: " + " | ".join(parts)

    def _dashboard_lines(self) -> list[str]:
        content_width = max(24, self.width - 6)
        lines = []

        lines.extend(self._wrap_label_value("Overview", self.project_summary, content_width, max_lines=2))

        watch_text = self._watch_dashboard_line().replace("Watch: ", "", 1)
        lines.append(f"Watch {watch_text} | Memory {self._memory_status_text()}")
        lines.append(
            f"Scope {self.ask_scope.title()} | Sync {self._relative_time(self.last_sync_at)} | Smoke {WATCHER_SMOKE_MARKER}"
        )
        lines.append(self._watch_insight_line())

        if self.last_question:
            lines.extend(self._wrap_label_value("Q", self.last_question, content_width, max_lines=2))
        else:
            lines.append("Q: none yet")

        answer_section = self.last_answer_sections.get("Answer", [])
        confidence_section = self.last_answer_sections.get("Confidence", [])
        if answer_section:
            answer = " ".join(self._clean_section_value(item) for item in answer_section if str(item or "").strip()).strip()
            lines.extend(self._wrap_label_value("A", answer, content_width, max_lines=2))
            if confidence_section:
                confidence = " ".join(
                    self._clean_section_value(item) for item in confidence_section[:1] if str(item or "").strip()
                ).strip()
                lines.append(f"Confidence: {confidence}")
        elif self.last_answer_lines:
            answer = " ".join(self.last_answer_lines).strip()
            lines.extend(self._wrap_label_value("A", answer, content_width, max_lines=2))
        else:
            lines.append("A: none yet")

        return lines

    def draw(self) -> None:
        self._sync_menu_mode()
        self.stdscr.erase()
        self.height, self.width = self.stdscr.getmaxyx()
        actions = self._visible_actions()
        dashboard_lines, actions_top, logs_top = self._layout_metrics(actions)
        min_height = logs_top + 3
        min_width = 72

        if self.height < min_height or self.width < min_width:
            msg = f"Terminal too small ({self.width}x{self.height}). Resize to at least {min_width}x{min_height}."
            self.stdscr.addnstr(0, 0, msg, self.width - 1)
            self.stdscr.refresh()
            return

        base_title = "Brain Dashboard" if self.onboarding_complete else "Brain Setup"
        title = f"{base_title} v{self.brain_version}"
        subtitle = f"Project: {self.project_root}"
        help_line = "Move: arrows/jk | Run: Enter | Quit: q"
        if self.runtime:
            help_line = "Running command | Cancel: x | Quit: q after return"
        elif self.output_override_lines:
            help_line = "Move: arrows/jk | Run: Enter | Scroll: PgUp/PgDn Home/End | Quit: q"
        runtime_line = self._runtime_line()

        self.stdscr.addnstr(0, 2, title, self.width - 4, curses.A_BOLD)
        self.stdscr.addnstr(1, 2, subtitle, self.width - 4)
        self.stdscr.addnstr(2, 2, help_line, self.width - 4)
        self.stdscr.addnstr(3, 2, runtime_line, self.width - 4)

        if self.onboarding_complete:
            dashboard_top = 5
            self.stdscr.hline(dashboard_top, 1, curses.ACS_HLINE, self.width - 2)
            self.stdscr.addnstr(dashboard_top, 3, " Dashboard ", self.width - 6, curses.A_BOLD)
            for idx, line in enumerate(dashboard_lines):
                self.stdscr.addnstr(dashboard_top + 1 + idx, 2, line, self.width - 4)

        for idx, (key, label) in enumerate(actions):
            y = actions_top + idx
            attr = curses.A_REVERSE if idx == self.selected else curses.A_NORMAL
            if self._is_setup_action(key):
                line = f"{self._status_label(key)} {label}"
            else:
                line = f"    {label}"
            self.stdscr.addnstr(y, 2, line, self.width - 4, attr)

        self.stdscr.hline(logs_top, 1, curses.ACS_HLINE, self.width - 2)
        log_height = max(1, self.height - logs_top - 2)
        self.stdscr.addnstr(logs_top, 3, self._output_status_label(log_height), self.width - 6, curses.A_BOLD)

        visible = self._visible_output_lines(log_height)
        for i, line in enumerate(visible):
            self.stdscr.addnstr(logs_top + 1 + i, 2, line, self.width - 4)

        self.stdscr.refresh()

    def _brain_cmd(self, args) -> list[str]:
        cli_script = Path(__file__).with_name("brain_cli.py")
        return [sys.executable, str(cli_script)] + list(args)

    def _run_command(self, args, key: str, show_command: bool = True) -> bool:
        self.states[key].status = "running"
        self.states[key].detail = ""
        self.output_override_lines = []
        self.runtime = {
            "name": " ".join(args),
            "key": key,
            "started_at": time.time(),
            "spinner_index": 0,
            "progress_current": 0,
            "progress_total": 0,
            "detail": "",
            "captured_lines": [],
        }
        self.draw()

        cmd = self._brain_cmd(args)
        if show_command:
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
        if key == "sync" and ok:
            self.last_sync_at = time.time()
        if key == "ask":
            captured = [str(line).strip() for line in self.runtime.get("captured_lines", []) if str(line).strip()]
            if captured:
                self.last_answer_lines = captured
                self.last_answer_sections = self._parse_answer_sections(captured)
                self._show_answer_output()
        if not ok:
            self.append_log(f"Command failed ({return_code}).")
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
        if not ok:
            self.append_log(f"Background watch exited ({return_code}).")
        return False

    def _handle_runtime_output(self, raw_line: str) -> None:
        line = str(raw_line or "").rstrip("\n")
        if not line:
            return

        runtime_key = str((self.runtime or {}).get("key", "")).strip()

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
            return

        if runtime_key == "sync":
            if line.startswith("Project memory updated:"):
                self.append_log("Sync complete.")
                return
            if line.startswith("Chroma write issue detected."):
                self.append_log(line)
                return
            return

        if runtime_key == "ask":
            if line.startswith("OpenAI summarization skipped:"):
                return
            if line == "Human Summary:":
                return
            if line.startswith("- Built from "):
                return
            if line.startswith("Codex Context:"):
                return
            if line.startswith("- Additional entries not shown:"):
                return
            if line.startswith("- "):
                line = line[2:]
            line = re.sub(r"^chat:[0-9a-f-]+:\s*", "", line)

        if runtime_key == "ask" and self.runtime is not None:
            self.runtime.setdefault("captured_lines", []).append(line)
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

    def _ask_text(self, prompt: str) -> str:
        self._suspend_for_input()
        try:
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

    def _open_env_for_key_setup(self) -> bool:
        script_path = self.project_root / "scripts" / "set_openai_key.sh"
        if not script_path.exists():
            self.append_log(f"Missing helper script: {script_path}")
            return False

        self._suspend_for_input()
        try:
            completed = subprocess.run(["bash", str(script_path)], check=False)
        finally:
            self._resume_after_input()

        if completed.returncode != 0:
            self.append_log("Could not prepare .env automatically.")
            return False
        return True

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

    def set_key(self):
        if not self._open_env_for_key_setup():
            self.states["key"].status = "fail"
            return

        if self._has_openai_key():
            self.append_log("OPENAI_API_KEY detected in .env")
            self.states["key"].status = "done"
            return

        self.append_log("Prepared .env. Open it in the editor pane and add OPENAI_API_KEY.")
        self.states["key"].status = "todo"

    def ask_question(self):
        question = self._ask_text("Question: ")
        if not question:
            self.append_log("No question entered.")
            self.states["ask"].status = "fail"
            return
        self.last_question = question
        self.last_answer_lines = []
        self.last_answer_sections = {}
        self._clear_output()
        ok = self._run_command(
            ["ask", "--scope", self.ask_scope, "--render", "sections", question],
            key="ask",
            show_command=False,
        )
        self.states["ask"].status = "done" if ok else "fail"

    def run_smoke_test(self):
        self.last_question = f"watcher smoke marker lookup ({WATCHER_SMOKE_QUERY})"
        self.last_answer_lines = []
        self.last_answer_sections = {}
        self._set_output_lines(
            [
                f"Smoke test query: {WATCHER_SMOKE_QUERY}",
                f"Expected marker: {WATCHER_SMOKE_MARKER}",
                "Expected source: WATCHER_SMOKE_TEST.md",
            ]
        )
        ok = self._run_command(
            ["ask", "--scope", "project", "--include-code", "--raw-only", WATCHER_SMOKE_QUERY],
            key="ask",
            show_command=False,
        )
        if ok:
            block = self._extract_raw_result_block(self.last_answer_lines, "WATCHER_SMOKE_TEST.md")
            if block:
                self._set_output_lines(
                    [
                        f"Smoke test query: {WATCHER_SMOKE_QUERY}",
                        f"Expected marker: {WATCHER_SMOKE_MARKER}",
                        "Matched source: WATCHER_SMOKE_TEST.md",
                        "",
                    ]
                    + block
                )
            else:
                self._set_output_lines(
                    [
                        f"Smoke test query: {WATCHER_SMOKE_QUERY}",
                        f"Expected marker: {WATCHER_SMOKE_MARKER}",
                        "WATCHER_SMOKE_TEST.md was not returned in the top raw results.",
                    ]
                    + self.last_answer_lines[:12]
                )
        self.states["ask"].status = "done" if ok else "fail"

    def show_guide(self):
        self._clear_output()
        self._run_command(["guide"], key="workflow", show_command=False)

    def map_task(self):
        question = self._ask_text("What do you want to change? ")
        if not question:
            self.append_log("No task entered.")
            return
        self._clear_output()
        self._run_command(["map", question], key="workflow", show_command=False)

    def summarize_work(self):
        self._clear_output()
        self._run_command(["summarize"], key="workflow", show_command=False)

    def run_release_check(self):
        self._clear_output()
        self._run_command(["release"], key="workflow", show_command=False)

    def start_watch(self):
        if self._is_watch_running():
            self.append_log("Watch mode is already running.")
            return

        cmd = self._brain_cmd(["watch"])
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
        self._show_watch_output()

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
        try:
            self.watch_status_path.parent.mkdir(parents=True, exist_ok=True)
            self.watch_status_path.write_text(
                json.dumps({"state": "stopped", "updated_at": int(time.time())}, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError:
            pass
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
        if key == "scope":
            order = ["mixed", "project", "chat"]
            try:
                idx = order.index(self.ask_scope)
            except ValueError:
                idx = 0
            self.ask_scope = order[(idx + 1) % len(order)]
            return True
        if key == "ask":
            self.ask_question()
            return True
        if key == "guide":
            self.show_guide()
            return True
        if key == "map":
            self.map_task()
            return True
        if key == "changes":
            self.summarize_work()
            return True
        if key == "release":
            self.run_release_check()
            return True
        if key == "smoke_test":
            self.run_smoke_test()
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
            if ch == curses.KEY_PPAGE and self.output_override_lines:
                log_height = self._output_log_height(actions)
                self.output_scroll = max(0, self.output_scroll - max(1, log_height - 1))
                continue
            if ch == curses.KEY_NPAGE and self.output_override_lines:
                log_height = self._output_log_height(actions)
                self.output_scroll = min(self._max_output_scroll(log_height), self.output_scroll + max(1, log_height - 1))
                continue
            if ch == curses.KEY_HOME and self.output_override_lines:
                self.output_scroll = 0
                continue
            if ch == curses.KEY_END and self.output_override_lines:
                log_height = self._output_log_height(actions)
                self.output_scroll = self._max_output_scroll(log_height)
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
