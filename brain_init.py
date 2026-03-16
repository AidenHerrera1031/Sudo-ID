from pathlib import Path

ENV_TEMPLATE = """# Optional but recommended for better summaries
OPENAI_API_KEY=

# Optional tuning
# BRAIN_OPENAI_TIMEOUT=8
# BRAIN_SYNC_PROGRESS=1
"""

BRAINIGNORE_TEMPLATE = """# Patterns excluded by `brain sync` and `brain watch`
# Supports *, ?, **, and ! negation.
#
# Examples:
# docs/archive/**
# *.log
# generated/
"""

BRAIN_TOML_TEMPLATE = """# Project-level Brain config

[index]
# Extra extensions to index. Defaults already include:
# .py, .js, .ts, .tsx, .md, .txt, .json, .yaml, .yml, .sh
include_extensions = []

# Extra directory names to ignore anywhere in the tree.
ignore_dirs = []

# Extra path patterns to ignore (same syntax as .brainignore).
ignore_patterns = []

[watch]
# Debounce delay before auto-sync runs after a file change.
debounce_seconds = 1.5
"""


def _write_if_needed(path: Path, content: str, force: bool) -> str:
    existed = path.exists()
    if existed and not force:
        return "skipped"
    path.write_text(content, encoding="utf-8")
    return "updated" if existed else "created"


def run_init(force: bool = False) -> int:
    root = Path(".").resolve()
    targets = [
        (root / ".env", ENV_TEMPLATE),
        (root / ".brainignore", BRAINIGNORE_TEMPLATE),
        (root / "brain.toml", BRAIN_TOML_TEMPLATE),
    ]

    created = 0
    updated = 0
    skipped = 0

    print(f"Initializing Brain files in: {root}")
    for path, template in targets:
        existed = path.exists()
        action = _write_if_needed(path, template, force)
        if action == "skipped":
            skipped += 1
            print(f"- skipped {path.name} (already exists)")
            continue
        if existed:
            updated += 1
            print(f"- updated {path.name}")
        else:
            created += 1
            print(f"- created {path.name}")

    print(f"Done. {created} created, {updated} updated, {skipped} skipped.")
    if skipped and not force:
        print("Use `brain init --force` to overwrite existing files.")
    return 0
