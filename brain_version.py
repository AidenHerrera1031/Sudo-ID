from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path

PACKAGE_NAME = "sudo-id-brain"
FALLBACK_VERSION = "0.2.3"


def _read_pyproject_version(pyproject_path: Path) -> str:
    try:
        lines = pyproject_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return ""

    in_project = False
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            in_project = line == "[project]"
            continue
        if in_project and line.startswith("version"):
            _key, _sep, value = line.partition("=")
            return value.strip().strip('"').strip("'")
    return ""


def _find_local_pyproject(start: Path) -> Path | None:
    for parent in [start.parent] + list(start.parents):
        candidate = parent / "pyproject.toml"
        if candidate.exists():
            return candidate
    return None


def find_local_source_root(start: Path | None = None) -> Path | None:
    module_path = (start or Path(__file__)).resolve()
    pyproject_path = _find_local_pyproject(module_path)
    if not pyproject_path:
        return None
    return pyproject_path.parent.resolve()


def get_brain_version() -> tuple[str, str]:
    module_path = Path(__file__).resolve()
    pyproject_path = _find_local_pyproject(module_path)
    if pyproject_path:
        local_version = _read_pyproject_version(pyproject_path)
        if local_version:
            return local_version, f"pyproject:{pyproject_path}"

    try:
        return package_version(PACKAGE_NAME), "package-metadata"
    except PackageNotFoundError:
        return FALLBACK_VERSION, "fallback"


def get_version_info(executable: str = "") -> dict[str, str]:
    version, source = get_brain_version()
    executable_path = ""
    if executable:
        try:
            executable_path = str(Path(executable).resolve())
        except OSError:
            executable_path = str(executable)
    source_root = find_local_source_root()
    return {
        "package_name": PACKAGE_NAME,
        "version": version,
        "source": source,
        "module_path": str(Path(__file__).resolve()),
        "executable": executable_path,
        "source_root": str(source_root) if source_root else "",
    }
