# Changelog

This file is the source-of-truth release log for Sudo-ID.

Release rule: every shipped update must do both of these:

- bump the package version in `pyproject.toml`
- add a matching entry to this file

## 0.2.3 - 2026-03-26

- removed stale tracked `build/lib` files that were causing `pipx` installs to package older CLI/TUI code
- added `build/` to `.gitignore` so future releases package the real source files

## 0.2.2 - 2026-03-26

- bumped package version so `brain version` reflects the latest shipped TUI/CLI update
- added an explicit changelog file to keep release versions tracked in-repo
