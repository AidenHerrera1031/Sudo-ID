import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

MODULE_DIR = str(Path(__file__).resolve().parent)
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

from brain_settings import load_settings, should_ignore_dir, should_include_file
from brain_workflows import analyze_change_set

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except Exception:
    FileSystemEventHandler = object
    Observer = None


def watch_status_path(project_root: Path) -> Path:
    return project_root / ".codex_brain" / "watch_status.json"


def _to_rel_path(path: Path, project_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(project_root.resolve())).replace("\\", "/")
    except Exception:
        return str(path).replace("\\", "/")


def write_watch_status(project_root: Path, **updates) -> None:
    path = watch_status_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {}
    try:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        payload = {}
    payload.update(updates)
    payload["updated_at"] = int(time.time())
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def run_sync_with_status(sync_cmd, project_root: Path, reason: str, changed_paths=None) -> bool:
    now = int(time.time())
    insights = analyze_change_set(changed_paths or [], project_root=project_root)
    write_watch_status(
        project_root,
        state="syncing",
        last_change_reason=reason,
        last_sync_started_at=now,
        **insights,
    )
    result = subprocess.run(sync_cmd, check=False)
    finished = int(time.time())
    ok = result.returncode == 0
    write_watch_status(
        project_root,
        state="running" if ok else "error",
        last_sync_started_at=now,
        last_sync_finished_at=finished,
        last_sync_ok=ok,
        last_sync_returncode=result.returncode,
        last_error="" if ok else f"sync exited with code {result.returncode}",
        **insights,
    )
    return ok


class BrainSyncHandler(FileSystemEventHandler):
    def __init__(self, sync_cmd, project_root: Path, settings, debounce_seconds=1.5):
        self.sync_cmd = sync_cmd
        self.project_root = project_root
        self.settings = settings
        self.debounce_seconds = debounce_seconds
        self.last_change = 0.0
        self.running = False
        self.lock = threading.Lock()
        self.changed_paths = set()

    def _should_handle(self, path: Path) -> bool:
        full_path = path if path.is_absolute() else self.project_root / path
        return should_include_file(full_path, project_root=self.project_root, settings=self.settings)

    def on_modified(self, event):
        self._schedule_sync(event)

    def on_created(self, event):
        self._schedule_sync(event)

    def on_moved(self, event):
        self._schedule_sync(event)

    def _schedule_sync(self, event):
        if event.is_directory:
            return

        moved_to = getattr(event, "dest_path", "")
        path = Path(moved_to or getattr(event, "src_path", ""))
        if not self._should_handle(path):
            return

        with self.lock:
            self.last_change = time.time()
            try:
                rel_path = str(path.resolve().relative_to(self.project_root.resolve())).replace("\\", "/")
            except Exception:
                rel_path = str(path).replace("\\", "/")
            self.changed_paths.add(rel_path)
            if self.running:
                return
            self.running = True
            changed_snapshot = sorted(self.changed_paths)
        write_watch_status(
            self.project_root,
            state="debouncing",
            last_change_at=int(time.time()),
            last_change_reason=str(path),
            **analyze_change_set(changed_snapshot, project_root=self.project_root),
        )

        threading.Thread(target=self._debounced_run, daemon=True).start()

    def _debounced_run(self):
        while True:
            time.sleep(self.debounce_seconds)
            with self.lock:
                elapsed = time.time() - self.last_change
            if elapsed >= self.debounce_seconds:
                break

        with self.lock:
            changed_paths = sorted(self.changed_paths)
            self.changed_paths.clear()

        print("Change detected. Running sync_brain.py ...")
        ok = run_sync_with_status(self.sync_cmd, self.project_root, "filesystem change", changed_paths=changed_paths)
        if ok:
            print("Sync complete. Watching for changes...")
        else:
            print("Sync failed. Watching for changes...")

        with self.lock:
            self.running = False


def iter_files(root: Path, project_root: Path, settings):
    for dirpath, dirnames, filenames in os.walk(root):
        current_dir = Path(dirpath)
        dirnames[:] = [
            d
            for d in dirnames
            if not should_ignore_dir(current_dir / d, project_root=project_root, settings=settings)
        ]
        for filename in filenames:
            path = Path(dirpath) / filename
            if should_include_file(path, project_root=project_root, settings=settings):
                yield path


def run_polling_watcher(watch_path: Path, project_root: Path, settings, sync_cmd, debounce_seconds: float) -> None:
    print("watchdog is not installed; using polling mode.")
    print(f"Watching {watch_path} for source/docs changes...")
    print("Press Ctrl+C to stop.")
    write_watch_status(
        project_root,
        state="running",
        backend="polling",
        watch_path=str(watch_path),
        last_error="",
    )

    last_snapshot = {}
    for path in iter_files(watch_path, project_root=project_root, settings=settings):
        try:
            last_snapshot[str(path)] = path.stat().st_mtime_ns
        except OSError:
            continue

    next_allowed_sync = 0.0
    try:
        while True:
            changed = False
            current = {}
            changed_paths = []
            for path in iter_files(watch_path, project_root=project_root, settings=settings):
                try:
                    mtime = path.stat().st_mtime_ns
                except OSError:
                    continue
                key = str(path)
                current[key] = mtime
                if key not in last_snapshot or last_snapshot[key] != mtime:
                    changed = True
                    changed_paths.append(_to_rel_path(path, project_root))

            if set(last_snapshot) != set(current):
                changed = True
                removed = set(last_snapshot) - set(current)
                changed_paths.extend(
                    _to_rel_path(Path(path), project_root)
                    for path in removed
                )

            now = time.time()
            if changed and now >= next_allowed_sync:
                print("Change detected. Running sync_brain.py ...")
                ok = run_sync_with_status(
                    sync_cmd,
                    project_root,
                    "filesystem change",
                    changed_paths=sorted({path for path in changed_paths if path}),
                )
                if ok:
                    print("Sync complete. Watching for changes...")
                else:
                    print("Sync failed. Watching for changes...")
                next_allowed_sync = now + debounce_seconds

            last_snapshot = current
            time.sleep(max(0.5, debounce_seconds))
    except KeyboardInterrupt:
        write_watch_status(project_root, state="stopped")
        return


def main():
    parser = argparse.ArgumentParser(description="Watch filesystem changes and auto-run sync_brain.py")
    parser.add_argument("--path", default=".", help="Directory to watch recursively")
    parser.add_argument(
        "--debounce",
        type=float,
        default=0.0,
        help="Debounce window in seconds (0 uses value from brain.toml or default).",
    )
    args = parser.parse_args()

    project_root = Path(".").resolve()
    settings = load_settings(project_root)
    for config_error in settings.config_errors:
        print(f"Config warning: {config_error}")

    watch_path = Path(args.path).resolve()
    sync_cmd = [sys.executable, str(Path(__file__).with_name("sync_brain.py"))]
    debounce_seconds = args.debounce if args.debounce > 0 else settings.watch_debounce_seconds

    if Observer is None:
        run_polling_watcher(
            watch_path,
            project_root=project_root,
            settings=settings,
            sync_cmd=sync_cmd,
            debounce_seconds=debounce_seconds,
        )
        return

    handler = BrainSyncHandler(
        sync_cmd=sync_cmd,
        project_root=project_root,
        settings=settings,
        debounce_seconds=debounce_seconds,
    )
    observer = Observer()
    observer.schedule(handler, str(watch_path), recursive=True)
    observer.start()
    write_watch_status(
        project_root,
        state="running",
        backend="watchdog",
        watch_path=str(watch_path),
        last_error="",
    )

    print(f"Watching {watch_path} for source/docs changes...")
    print("Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        write_watch_status(project_root, state="stopped")
    observer.join()


if __name__ == "__main__":
    main()
