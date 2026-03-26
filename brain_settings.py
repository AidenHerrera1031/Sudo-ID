import os
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

DEFAULT_INCLUDE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".md",
    ".txt",
    ".ini",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".sh",
}
DEFAULT_IGNORE_DIRS = {
    ".git",
    ".codex_brain",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    ".next",
    "dist",
    "build",
}
DEFAULT_WATCH_DEBOUNCE_SECONDS = 1.5


@dataclass
class BrainSettings:
    project_root: Path
    config_path: Path
    include_extensions: set[str] = field(default_factory=lambda: set(DEFAULT_INCLUDE_EXTENSIONS))
    ignore_dirs: set[str] = field(default_factory=lambda: set(DEFAULT_IGNORE_DIRS))
    ignore_patterns: list[str] = field(default_factory=list)
    watch_debounce_seconds: float = DEFAULT_WATCH_DEBOUNCE_SECONDS
    config_errors: list[str] = field(default_factory=list)


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        import tomllib

        parser = tomllib
    except ModuleNotFoundError:
        try:
            import tomli

            parser = tomli
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "TOML parser unavailable. Install tomli for Python < 3.11 to use brain.toml."
            ) from exc

    with path.open("rb") as handle:
        return parser.load(handle)


def _to_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def _to_rel_posix(path: Path, project_root: Path) -> str:
    try:
        rel = path.resolve().relative_to(project_root.resolve())
    except Exception:
        rel = path
    text = str(rel).replace("\\", "/")
    if text == ".":
        return ""
    if text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def _normalize_extensions(values: list[Any]) -> set[str]:
    exts = set(DEFAULT_INCLUDE_EXTENSIONS)
    for value in values:
        text = str(value or "").strip().lower()
        if not text:
            continue
        if not text.startswith("."):
            text = "." + text
        exts.add(text)
    return exts


def _merge_ignore_dirs(default_dirs: set[str], values: list[Any], ignore_patterns: list[str]) -> set[str]:
    out = set(default_dirs)
    for value in values:
        text = str(value or "").strip().strip("/")
        if not text:
            continue
        if "/" in text:
            ignore_patterns.append(f"{text}/")
            continue
        out.add(text)
    return out


def _load_brainignore(path: Path) -> list[str]:
    if not path.exists():
        return []

    patterns = []
    try:
        for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            patterns.append(line)
    except OSError:
        return []
    return patterns


def _match_pattern(rel_path: str, is_dir: bool, pattern: str) -> bool:
    pattern = pattern.strip()
    if not pattern:
        return False

    anchored = pattern.startswith("/")
    if anchored:
        pattern = pattern[1:]

    dir_only = pattern.endswith("/")
    if dir_only:
        pattern = pattern[:-1]
    if not pattern:
        return False

    has_wildcards = any(ch in pattern for ch in "*?[")
    path_parts = rel_path.split("/") if rel_path else []

    if dir_only:
        dir_path = rel_path if is_dir else "/".join(path_parts[:-1])
        if not dir_path:
            return False
        if anchored:
            if has_wildcards:
                return fnmatch(dir_path, pattern) or fnmatch(dir_path, f"{pattern}/*")
            return dir_path == pattern or dir_path.startswith(pattern + "/")

        if "/" not in pattern and not has_wildcards:
            return pattern in dir_path.split("/")

        return (
            fnmatch(dir_path, pattern)
            or fnmatch(dir_path, f"*/{pattern}")
            or dir_path == pattern
            or dir_path.endswith("/" + pattern)
            or ("/" + pattern + "/") in ("/" + dir_path + "/")
        )

    if anchored:
        return fnmatch(rel_path, pattern)

    if "/" not in pattern:
        if not path_parts:
            return False
        filename = path_parts[-1]
        return fnmatch(filename, pattern) or any(fnmatch(part, pattern) for part in path_parts)

    return (
        fnmatch(rel_path, pattern)
        or fnmatch(rel_path, f"*/{pattern}")
        or rel_path == pattern
        or rel_path.endswith("/" + pattern)
    )


def is_ignored_rel_path(rel_path: str, is_dir: bool, patterns: list[str]) -> bool:
    ignored = False
    for raw_pattern in patterns:
        pattern = str(raw_pattern or "").strip()
        if not pattern:
            continue
        negated = pattern.startswith("!")
        if negated:
            pattern = pattern[1:]
        if not pattern:
            continue
        if _match_pattern(rel_path, is_dir=is_dir, pattern=pattern):
            ignored = not negated
    return ignored


def should_ignore_dir(path: Path, project_root: Path, settings: BrainSettings) -> bool:
    rel_path = _to_rel_posix(path, project_root)
    if not rel_path:
        return False
    parts = rel_path.split("/")
    if any(part.endswith(".egg-info") for part in parts):
        return True
    if any(part in settings.ignore_dirs for part in parts):
        return True
    return is_ignored_rel_path(rel_path, is_dir=True, patterns=settings.ignore_patterns)


def should_include_file(path: Path, project_root: Path, settings: BrainSettings) -> bool:
    suffix = path.suffix.lower()
    if suffix not in settings.include_extensions:
        return False

    rel_path = _to_rel_posix(path, project_root)
    if not rel_path:
        return False

    parts = rel_path.split("/")
    if any(part.endswith(".egg-info") for part in parts[:-1]):
        return False
    if any(part in settings.ignore_dirs for part in parts[:-1]):
        return False

    if is_ignored_rel_path(rel_path, is_dir=False, patterns=settings.ignore_patterns):
        return False

    return True


def resolve_config_path(project_root: Path) -> Path:
    env_path = os.getenv("BRAIN_CONFIG_FILE", "").strip()
    if env_path:
        path = Path(env_path).expanduser()
        if not path.is_absolute():
            path = project_root / path
        return path
    return project_root / "brain.toml"


def load_settings(project_root: Path | None = None) -> BrainSettings:
    root = (project_root or Path(".")).resolve()
    config_path = resolve_config_path(root)

    settings = BrainSettings(
        project_root=root,
        config_path=config_path,
        include_extensions=set(DEFAULT_INCLUDE_EXTENSIONS),
        ignore_dirs=set(DEFAULT_IGNORE_DIRS),
        ignore_patterns=[],
        watch_debounce_seconds=DEFAULT_WATCH_DEBOUNCE_SECONDS,
        config_errors=[],
    )

    if config_path.exists():
        try:
            parsed = _load_toml(config_path)
            index_cfg = parsed.get("index") if isinstance(parsed, dict) else None
            if isinstance(index_cfg, dict):
                settings.include_extensions = _normalize_extensions(
                    _to_list(index_cfg.get("include_extensions"))
                )
                settings.ignore_dirs = _merge_ignore_dirs(
                    set(DEFAULT_IGNORE_DIRS),
                    _to_list(index_cfg.get("ignore_dirs")),
                    settings.ignore_patterns,
                )
                for value in _to_list(index_cfg.get("ignore_patterns")):
                    pattern = str(value or "").strip()
                    if pattern:
                        settings.ignore_patterns.append(pattern)

            watch_cfg = parsed.get("watch") if isinstance(parsed, dict) else None
            if isinstance(watch_cfg, dict):
                raw_debounce = watch_cfg.get("debounce_seconds", DEFAULT_WATCH_DEBOUNCE_SECONDS)
                try:
                    settings.watch_debounce_seconds = max(0.1, float(raw_debounce))
                except (TypeError, ValueError):
                    settings.config_errors.append(
                        f"Invalid watch.debounce_seconds value in {config_path}; using default {DEFAULT_WATCH_DEBOUNCE_SECONDS}."
                    )
        except Exception as exc:
            settings.config_errors.append(f"Could not parse {config_path}: {exc}")

    brainignore_path = root / ".brainignore"
    settings.ignore_patterns.extend(_load_brainignore(brainignore_path))
    return settings
