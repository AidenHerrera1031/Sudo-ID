import json
import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

import brain_init
from brain_common import DB_PATH, get_collection, probe_collection, reset_collection
from brain_settings import load_settings


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str
    hint: str = ""


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


def _check_python() -> CheckResult:
    version = sys.version_info
    current = platform.python_version()
    if version >= (3, 9):
        return CheckResult("Python", "ok", f"{current}")
    return CheckResult("Python", "fail", f"{current}", "Upgrade to Python 3.9+.")


def _check_dependencies() -> CheckResult:
    required = ["chromadb", "dotenv"]
    optional = ["openai", "watchdog"]
    missing_required = []
    missing_optional = []

    for module_name in required:
        try:
            __import__(module_name)
        except Exception:
            missing_required.append(module_name)

    for module_name in optional:
        try:
            __import__(module_name)
        except Exception:
            missing_optional.append(module_name)

    if missing_required:
        return CheckResult(
            "Dependencies",
            "fail",
            f"Missing required modules: {', '.join(missing_required)}",
            "Run your setup/install command to install dependencies.",
        )

    if missing_optional:
        return CheckResult(
            "Dependencies",
            "warn",
            f"Missing optional modules: {', '.join(missing_optional)}",
            "Install optional modules for improved quality/features.",
        )

    return CheckResult("Dependencies", "ok", "All core and optional modules available.")


def _check_env() -> CheckResult:
    env_path = Path(".env")
    has_key = _has_openai_key()
    if has_key:
        source = "environment/.env"
        return CheckResult("OPENAI_API_KEY", "ok", f"Configured via {source}.")

    if env_path.exists():
        return CheckResult(
            "OPENAI_API_KEY",
            "warn",
            ".env exists but OPENAI_API_KEY is empty.",
            "Run `npm run set-key` or set OPENAI_API_KEY in your shell.",
        )

    return CheckResult(
        "OPENAI_API_KEY",
        "warn",
        ".env is missing and OPENAI_API_KEY is not set.",
        "Run `brain init` or create .env manually.",
    )


def _check_settings() -> list[CheckResult]:
    settings = load_settings(Path(".").resolve())
    results = []
    config_path = settings.config_path
    if config_path.exists():
        results.append(CheckResult("Config", "ok", f"Loaded {config_path.name}"))
    else:
        results.append(
            CheckResult(
                "Config",
                "warn",
                f"{config_path.name} not found.",
                "Run `brain init` to scaffold defaults.",
            )
        )

    ignore_path = Path(".brainignore")
    if ignore_path.exists():
        results.append(CheckResult(".brainignore", "ok", "Found project ignore file."))
    else:
        results.append(
            CheckResult(
                ".brainignore",
                "warn",
                "No .brainignore file found.",
                "Run `brain init` to create one.",
            )
        )

    for error in settings.config_errors:
        results.append(CheckResult("Config", "warn", error, "Fix brain.toml syntax/values."))

    results.append(
        CheckResult(
            "Index Rules",
            "ok",
            f"{len(settings.include_extensions)} extensions, {len(settings.ignore_dirs)} ignored dir names, "
            f"{len(settings.ignore_patterns)} ignore patterns.",
        )
    )
    return results


def _check_db_path() -> CheckResult:
    db_path = Path(DB_PATH).expanduser()
    try:
        db_path.mkdir(parents=True, exist_ok=True)
        probe = db_path / ".doctor_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return CheckResult("DB Path", "ok", f"Writable: {db_path}")
    except Exception as exc:
        return CheckResult(
            "DB Path",
            "fail",
            f"Cannot write to {db_path}: {exc}",
            "Set BRAIN_DB_PATH to a writable directory.",
        )


def _check_collection() -> CheckResult:
    ok, detail = probe_collection()
    if not ok:
        return CheckResult(
            "Collection",
            "fail",
            f"Collection probe failed: {detail}",
            "Run `brain doctor --fix` or `brain sync --force-reindex` to rebuild the active collection.",
        )
    try:
        collection = get_collection()
        count = collection.count()
        return CheckResult("Collection", "ok", f"Accessible ({count} records).")
    except Exception as exc:
        return CheckResult(
            "Collection",
            "fail",
            f"Could not open collection: {exc}",
            "Run `brain sync --force-reindex` after fixing DB path/permissions.",
        )


def _check_index_state() -> CheckResult:
    state_file = Path(DB_PATH) / "index_state.json"
    if state_file.exists():
        return CheckResult("Index State", "ok", f"Found {state_file}")
    return CheckResult(
        "Index State",
        "warn",
        f"Missing {state_file}",
        "Run `brain sync` to build initial index state.",
    )


def apply_fixes(results: list[CheckResult]) -> list[str]:
    actions = []
    names = {item.name: item for item in results}

    config_result = names.get("Config")
    ignore_result = names.get(".brainignore")
    env_result = names.get("OPENAI_API_KEY")
    if (
        (config_result and config_result.status == "warn")
        or (ignore_result and ignore_result.status == "warn")
        or (env_result and ".env is missing" in env_result.detail)
    ):
        brain_init.run_init(force=False)
        actions.append("Scaffolded missing Brain project files with `brain init`.")

    db_path = Path(DB_PATH).expanduser()
    try:
        db_path.mkdir(parents=True, exist_ok=True)
        actions.append(f"Ensured DB directory exists: {db_path}")
    except Exception:
        pass

    collection_result = names.get("Collection")
    if collection_result and collection_result.status == "fail":
        try:
            reset_collection()
            actions.append("Reset the active Chroma collection after a failed collection probe.")
        except Exception:
            pass

    return actions


def run_doctor(json_output: bool = False, fix: bool = False) -> int:
    results = []
    results.append(_check_python())
    results.append(_check_dependencies())
    results.append(_check_env())
    results.extend(_check_settings())
    results.append(_check_db_path())
    results.append(_check_collection())
    results.append(_check_index_state())
    fix_actions = apply_fixes(results) if fix else []

    fail_count = sum(1 for item in results if item.status == "fail")
    warn_count = sum(1 for item in results if item.status == "warn")
    ok_count = sum(1 for item in results if item.status == "ok")

    if json_output:
        payload = {
            "ok": ok_count,
            "warn": warn_count,
            "fail": fail_count,
            "checks": [
                {
                    "name": item.name,
                    "status": item.status,
                    "detail": item.detail,
                    "hint": item.hint,
                }
                for item in results
            ],
            "fix_actions": fix_actions,
        }
        print(json.dumps(payload, indent=2))
    else:
        print("Brain Doctor")
        for item in results:
            label = item.status.upper().ljust(4)
            print(f"[{label}] {item.name}: {item.detail}")
            if item.hint:
                print(f"       hint: {item.hint}")
        if fix_actions:
            print("")
            print("Fix Actions:")
            for action in fix_actions:
                print(f"- {action}")
        print("")
        print(f"Summary: {ok_count} ok, {warn_count} warnings, {fail_count} failures")

    return 1 if fail_count else 0
