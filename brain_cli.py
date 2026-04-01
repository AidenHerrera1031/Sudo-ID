import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

MODULE_DIR = str(Path(__file__).resolve().parent)
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import ask_brain
import brain_doctor
import brain_init
import brain_tui
import brain_version
import memorize
import sync_brain
import watch_brain
import brain_workflows


def _invoke_main(main_fn, argv, prog):
    old_argv = sys.argv
    try:
        sys.argv = [prog] + argv
        main_fn()
    finally:
        sys.argv = old_argv


def sync_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="brain sync",
        description="Sync project files and chat context into local memory store.",
    )
    parser.add_argument(
        "--force-reindex",
        action="store_true",
        help="Reset state assumptions and force full reindex.",
    )
    args = parser.parse_args(argv)
    sync_brain.run_sync(force_reindex=args.force_reindex)


def ask_main(argv=None):
    _invoke_main(ask_brain.main, list(argv or []), "brain ask")


def watch_main(argv=None):
    _invoke_main(watch_brain.main, list(argv or []), "brain watch")


def remember_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="brain remember",
        description="Store a distilled memory note from provided text or stdin.",
    )
    parser.add_argument(
        "--text",
        default="",
        help="Optional note text. If omitted, reads from stdin.",
    )
    args = parser.parse_args(argv)

    text = (args.text or "").strip()
    if not text:
        text = sys.stdin.read().strip()
    if not text:
        print("Usage: brain remember --text \"...\"  or pipe text into stdin.")
        return
    memorize.extract_and_store(text)


def init_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="brain init",
        description="Create starter Brain config files in the current project.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing .env/.brainignore/brain.toml files.",
    )
    args = parser.parse_args(argv)
    raise SystemExit(brain_init.run_init(force=args.force))


def doctor_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="brain doctor",
        description="Run local health checks for Brain configuration and storage.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Apply safe local fixes for missing starter files and storage paths.",
    )
    args = parser.parse_args(argv)
    raise SystemExit(brain_doctor.run_doctor(json_output=args.json, fix=args.fix))


def tui_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="brain tui",
        description="Launch the interactive terminal setup UI.",
    )
    parser.parse_args(argv)
    raise SystemExit(brain_tui.run_tui())


def version_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="brain version",
        description="Show the running Brain version and install/source details.",
    )
    parser.add_argument(
        "--short",
        action="store_true",
        help="Print only the version number.",
    )
    args = parser.parse_args(argv)

    info = brain_version.get_version_info(executable=shutil.which("brain") or sys.argv[0])
    if args.short:
        print(info["version"])
        return

    print(f"Brain version: {info['version']}")
    print(f"Version source: {info['source']}")
    if info.get("source_root"):
        print(f"Source repo: {info['source_root']}")
    print(f"Executable: {info['executable'] or 'unknown'}")
    print(f"Module path: {info['module_path']}")


def guide_main(argv=None):
    brain_workflows.guide_main(argv)


def map_main(argv=None):
    brain_workflows.map_main(argv)


def refactor_main(argv=None):
    brain_workflows.refactor_main(argv)


def summarize_main(argv=None):
    brain_workflows.summarize_main(argv)


def handoff_main(argv=None):
    brain_workflows.handoff_main(argv)


def pr_main(argv=None):
    brain_workflows.pr_main(argv)


def decision_main(argv=None):
    brain_workflows.decision_main(argv)


def release_main(argv=None):
    brain_workflows.release_main(argv)


def install_shell_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="brain install-shell",
        description="Print or append shell PATH setup so the `brain` command is available.",
    )
    parser.add_argument(
        "--shell",
        choices=["bash", "zsh"],
        default="bash",
        help="Shell profile to target.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Append the snippet to the shell rc file instead of printing it.",
    )
    args = parser.parse_args(argv)

    resolved_brain = shutil.which("brain")
    brain_path = Path(resolved_brain).resolve() if resolved_brain else Path(sys.argv[0]).resolve()
    install_dir = brain_path.parent
    snippet = f'export PATH="{install_dir}:$PATH"'
    rc_path = Path.home() / (".zshrc" if args.shell == "zsh" else ".bashrc")

    if not args.write:
        print(snippet)
        print(f"# Add the line above to {rc_path}")
        return

    existing = ""
    try:
        existing = rc_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        existing = ""
    if snippet not in existing:
        new_content = existing.rstrip("\n")
        if new_content:
            new_content += "\n"
        new_content += snippet + "\n"
        rc_path.write_text(new_content, encoding="utf-8")
        print(f"Updated {rc_path}")
    else:
        print(f"{rc_path} already contains the Brain PATH export.")


def upgrade_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="brain upgrade",
        description="Install or update Brain from a local repo path or pipx-installable source.",
    )
    parser.add_argument(
        "--source",
        default="",
        help="Local path or pipx-installable source. If omitted, reuse the detected Brain source repo.",
    )
    parser.add_argument(
        "--editable",
        action="store_true",
        help="Install from a local repo in editable mode so `brain` uses the live source checkout.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the command without executing it.",
    )
    args = parser.parse_args(argv)

    source = (args.source or "").strip()
    if not source:
        source_root = brain_version.find_local_source_root()
        if not source_root:
            print(
                "Could not detect a local Brain source repo from the current install.",
                file=sys.stderr,
            )
            print(
                "Pass --source /path/to/Sudo-ID or another pipx-installable package reference.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        source = str(source_root)

    source_path = Path(source).expanduser()
    install_target = str(source_path.resolve()) if source_path.exists() else source
    cmd = ["pipx", "install", "--force", install_target]
    if args.editable:
        if not source_path.exists():
            print("--editable requires a local source path.", file=sys.stderr)
            raise SystemExit(2)
        cmd.insert(2, "--editable")

    if args.dry_run:
        print(" ".join(cmd))
        return

    raise SystemExit(subprocess.run(cmd, check=False).returncode)


def _prompt_yes_no(message: str, default: bool = True) -> bool:
    if not sys.stdin.isatty():
        return default

    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        raw = input(f"{message} {suffix} ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print("Please answer with 'y' or 'n'.")


def _has_openai_key() -> bool:
    if os.getenv("OPENAI_API_KEY", "").strip():
        return True

    env_path = Path(".env")
    if not env_path.exists():
        return False

    try:
        values = dotenv_values(env_path)
    except Exception:
        return False
    return bool(str(values.get("OPENAI_API_KEY", "")).strip())


def _open_env_for_key_setup() -> int:
    script_path = Path(__file__).resolve().parent / "scripts" / "set_openai_key.sh"
    if not script_path.exists():
        print(f"Missing helper script: {script_path}", file=sys.stderr)
        return 1
    return subprocess.run(["bash", str(script_path)], check=False).returncode


def start_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="brain start",
        description="Guided project onboarding: init, doctor, sync, and optional query/watch.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Run recommended non-interactive path (no watch start).",
    )
    parser.add_argument(
        "--no-watch",
        action="store_true",
        help="Skip watcher prompt/start.",
    )
    parser.add_argument(
        "--question",
        default="",
        help="Optional question to run with `brain ask` after sync.",
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        help="Launch the interactive terminal UI instead of prompt-by-prompt mode.",
    )
    args = parser.parse_args(argv)

    if args.tui:
        raise SystemExit(brain_tui.run_tui())

    project_root = Path(".").resolve()
    print(f"Brain guided start in: {project_root}")
    print("")
    print("Step 1/5: Initialize starter files")
    brain_init.run_init(force=False)
    print("")

    print("Step 2/5: Run health checks")
    doctor_exit = brain_doctor.run_doctor(json_output=False)
    if doctor_exit != 0 and not (args.yes or _prompt_yes_no("Doctor found failures. Continue anyway?", default=False)):
        raise SystemExit(1)
    print("")

    if not _has_openai_key():
        if args.yes:
            print("Step 3/5: OPENAI_API_KEY not found. Skipping key prompt in --yes mode.")
            print("         You can set it later with: npm run set-key")
        elif _prompt_yes_no("Step 3/5: OPENAI_API_KEY not found. Open .env now?", default=True):
            if _open_env_for_key_setup() != 0:
                print("Could not prepare .env automatically. Run `npm run set-key` later.")
            elif _has_openai_key():
                print("OPENAI_API_KEY detected in .env.")
            else:
                print("Prepared .env. Add OPENAI_API_KEY there when ready.")
        else:
            print("Skipping API key setup.")
    else:
        print("Step 3/5: OPENAI_API_KEY already configured.")
    print("")

    run_sync_now = args.yes or _prompt_yes_no("Step 4/5: Run initial sync now?", default=True)
    sync_ok = False
    if run_sync_now:
        try:
            sync_brain.run_sync(force_reindex=False)
            sync_ok = True
        except Exception as exc:
            print("")
            print(f"Sync failed: {exc}")
            print("Try again with `brain sync` (or `brain sync --force-reindex`).")
            if not (args.yes or _prompt_yes_no("Continue setup anyway?", default=True)):
                raise SystemExit(1)
    else:
        print("Skipped sync. Run `brain sync` when ready.")
    print("")

    question = (args.question or "").strip()
    if not question and not args.yes and _prompt_yes_no("Step 5/5: Ask a quick question now?", default=True):
        question = input("Enter your question: ").strip()
    if question and sync_ok:
        ask_main([question])
        print("")
    elif question and not sync_ok:
        print("Skipping question because sync did not complete.")
        print("")

    if args.no_watch:
        print("Skipping watcher start (--no-watch).")
        raise SystemExit(0)

    if not args.yes and _prompt_yes_no("Start watcher now? (runs until Ctrl+C)", default=False):
        watch_main([])
        return

    print("Setup complete. Next command: `brain watch`")
    raise SystemExit(0)


def _print_usage():
    print("Usage: brain <command> [args]")
    print("")
    print("Commands:")
    print("  version   Show the running Brain version and install details")
    print("  start     Guided setup wizard (recommended first run)")
    print("  tui       Full-screen terminal UI for setup and operations")
    print("  init      Scaffold .env, .brainignore, and brain.toml")
    print("  doctor    Run environment/config/storage diagnostics")
    print("  upgrade   Reinstall/update the Brain CLI with pipx")
    print("  install-shell Print or append shell PATH setup for `brain`")
    print("  sync      Sync project + chat context into local memory")
    print("  ask       Query memory context")
    print("  watch     Auto-sync on file changes")
    print("  remember  Save a distilled memory note")
    print("  guide     Guided repo walkthrough for new contributors")
    print("  map       Map a task or question to likely files/functions")
    print("  refactor  Show impact and verification guidance before edits")
    print("  summarize Summarize current work, handoff, or PR context")
    print("  handoff   Prepare a compact handoff summary")
    print("  pr        Generate reviewer-facing PR context")
    print("  decision  Store or list durable project memory")
    print("  release   Run pre-release readiness checks")
    print("")
    print("Examples:")
    print("  brain start")
    print("  brain version")
    print("  brain --version")
    print("  brain start --tui")
    print("  brain tui")
    print("  brain init")
    print("  brain doctor")
    print("  brain doctor --fix")
    print("  brain upgrade --dry-run")
    print("  brain upgrade --source /workspaces/Sudo-ID --editable")
    print("  brain install-shell")
    print("  brain sync")
    print("  brain ask \"what changed today?\"")
    print("  brain watch")
    print("  brain remember --text \"Decision: ...\"")
    print("  brain guide")
    print("  brain map \"watcher status\"")
    print("  brain refactor \"sync progress output\"")
    print("  brain summarize --mode handoff")
    print("  brain decision --kind rule --title \"Docs first\" --text \"Update COMMANDS.md with CLI changes\"")
    print("  brain release")


def main():
    if len(sys.argv) >= 2 and sys.argv[1] in {"-V", "--version"}:
        version_main([])
        return

    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help", "help"}:
        _print_usage()
        return

    command = sys.argv[1].strip().lower()
    argv = sys.argv[2:]

    if command == "version":
        version_main(argv)
        return
    if command == "start":
        start_main(argv)
        return
    if command == "tui":
        tui_main(argv)
        return
    if command == "init":
        init_main(argv)
        return
    if command == "doctor":
        doctor_main(argv)
        return
    if command == "upgrade":
        upgrade_main(argv)
        return
    if command == "install-shell":
        install_shell_main(argv)
        return
    if command == "sync":
        sync_main(argv)
        return
    if command == "ask":
        ask_main(argv)
        return
    if command == "watch":
        watch_main(argv)
        return
    if command == "remember":
        remember_main(argv)
        return
    if command == "guide":
        guide_main(argv)
        return
    if command == "map":
        map_main(argv)
        return
    if command == "refactor":
        refactor_main(argv)
        return
    if command == "summarize":
        summarize_main(argv)
        return
    if command == "handoff":
        handoff_main(argv)
        return
    if command == "pr":
        pr_main(argv)
        return
    if command == "decision":
        decision_main(argv)
        return
    if command == "release":
        release_main(argv)
        return

    print(f"Unknown command: {command}", file=sys.stderr)
    _print_usage()
    raise SystemExit(2)


if __name__ == "__main__":
    main()
