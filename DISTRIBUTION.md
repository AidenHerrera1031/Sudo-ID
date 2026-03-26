# Sudo-ID Brain Distribution Guide

## What is now included

This repo now supports an installable CLI app for cross-project use:

- `brain sync`
- `brain ask`
- `brain watch`
- `brain remember`
- `brain guide`
- `brain map`
- `brain refactor`
- `brain summarize`
- `brain release`
- `brain decision`
- `brain start`
- `brain tui`
- `brain init`
- `brain doctor`

Packaging and entrypoints:
- `pyproject.toml`
- `brain_cli.py`

## Install for other users

After publishing this repo:

```bash
pipx install "git+https://github.com/<org>/<repo>.git"
```

From a local clone:

```bash
cd /path/to/repo
pipx install .
```

For local development where other repos should use the latest code from this checkout:

```bash
cd /workspaces/Sudo-ID
brain upgrade --source /workspaces/Sudo-ID --editable
```

## Use in any project

```bash
cd /path/to/any-project
brain start
brain tui
brain sync
brain ask "What does this project do?"
brain watch
brain remember --text "Decision: ..."
brain guide
brain map "watcher status"
brain summarize
brain release
```

Run `brain version` to confirm the install points at this source tree.

Also available:
- `brain-sync`
- `brain-ask`
- `brain-watch`
- `brain-remember`
- `brain-guide`
- `brain-map`
- `brain-refactor`
- `brain-summarize`
- `brain-release`
- `brain-decision`
- `brain-start`
- `brain-tui`
- `brain-init`
- `brain-doctor`

## Project config and ignore rules

Use `brain init` in any project to scaffold:

- `.env`
- `.brainignore`
- `brain.toml`

`.brainignore` patterns are applied by both `brain sync` and `brain watch` so you can skip noisy paths.

## Multi-project configuration

Environment variables:

- `BRAIN_DB_PATH` (default: `./.codex_brain`)
- `BRAIN_COLLECTION_BASE_NAME` (default: `project_context`)
- `BRAIN_CONFIG_FILE` (optional path to a non-default `brain.toml`)

Example:

```bash
export BRAIN_DB_PATH="$PWD/.codex_brain"
brain sync
```

## Runtime notes

- `brain sync` uses the same safe sync flow as `sync_brain.py`, including Chroma recovery handling.
- If Chroma backend errors appear (`Failed to apply logs...`), rerun sync; the recovery path attempts rebuild and retry.
