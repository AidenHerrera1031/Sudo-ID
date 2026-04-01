from pathlib import Path

ENV_TEMPLATE = """# Optional but recommended for better summaries
# This file stores local secrets and should stay out of git.
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


def ensure_env_gitignored(root: Path) -> str:
    gitignore_path = root / ".gitignore"
    entry = ".env"
    existed = gitignore_path.exists()

    if existed:
        try:
            existing_lines = gitignore_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            existing_lines = []
    else:
        existing_lines = []

    if any(line.strip() == entry for line in existing_lines):
        return "skipped"

    lines = list(existing_lines)
    if lines:
        lines.append("")
    lines.append(entry)
    gitignore_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
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

    gitignore_action = ensure_env_gitignored(root)
    if gitignore_action == "created":
        created += 1
        print("- created .gitignore (with .env ignored)")
    elif gitignore_action == "updated":
        updated += 1
        print("- updated .gitignore (added .env)")
    else:
        skipped += 1
        print("- skipped .gitignore (.env already ignored)")

    print(f"Done. {created} created, {updated} updated, {skipped} skipped.")
    if skipped and not force:
        print("Use `brain init --force` to overwrite existing files.")
    return 0
