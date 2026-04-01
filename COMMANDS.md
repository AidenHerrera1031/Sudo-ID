# Sudo-ID Commands and Sync Logic

## App-style commands (for any project)

Install once (from a clone of this repo):

```bash
cd /path/to/sudo-id-repo
pipx install .
```

For a local checkout that should stay live across other repos:

```bash
cd /workspaces/Sudo-ID
brain upgrade --source /workspaces/Sudo-ID --editable
```

Then in any project:

```bash
cd /path/to/other-project
brain start
brain tui
brain sync
brain ask "What changed?"
brain watch
brain remember --text "Decision: ..."
brain guide
brain map "watcher status"
brain refactor "sync progress output"
brain summarize
brain handoff
brain pr
brain decision --text "Keep watcher insights compact for beginners"
brain release
brain init
brain doctor
```

Use `brain version` to verify the active source. If linked to this checkout, it prints `Source repo: /workspaces/Sudo-ID`.

Equivalent standalone commands are also available:
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

## One-time setup

```bash
cd /workspaces/Sudo-ID
npm run setup
```

What it does:
- Creates `.venv` if missing.
- Installs Python deps from `requirements.txt` if needed.
- Creates `.env` if missing.
- Runs an initial `sync_brain.py` (unless setup is called internally with `--skip-sync`).

## Recommended daily start

```bash
cd /workspaces/Sudo-ID
npm start
```

What `npm start` does:
- Runs setup (without initial sync duplication).
- Runs one full sync immediately.
- Starts file watcher and keeps running until you stop it (`Ctrl+C`).

## Core commands

```bash
npm run set-key
```
Prepare `.env` so you can set `OPENAI_API_KEY` manually in the editor pane. If `.env` does not exist, this also scaffolds the file and adds `.env` to `.gitignore`.

```bash
npm run guide
```
Run the guided start wizard (`brain start`).

```bash
npm run tui
```
Launch the full-screen terminal UI (`brain tui`). Running steps show a live progress strip; press `x` to cancel the current run.

```bash
npm run init
```
Create `.env`, `.brainignore`, and `brain.toml` starter files.

```bash
npm run doctor
```
Run local health checks for config, dependencies, and DB access.

```bash
npm run sync
```
Run one full sync now.

```bash
npm run watch
```
Watch files and auto-run sync after changes.

```bash
npm run ask -- "What changed in the project?"
```
Query indexed memory.

```bash
npm run remember
```
Paste notes, then press `Ctrl+D` to save them.

```bash
brain guide
```
Print a guided repo walkthrough with core files, flows, setup files, and durable project memory.

```bash
brain map "where do I change watcher status?"
```
Return the most likely files, symbols, and impact areas for a task.

```bash
brain refactor "sync progress output"
```
Show likely impact files, regression areas, and tests/manual checks to run before a refactor.

```bash
brain summarize --mode work
brain handoff
brain pr
```
Generate current-work summaries, handoff notes, or reviewer-facing PR context from the worktree/latest diff.

```bash
brain decision --kind convention --title "Docs first" --text "Update README and COMMANDS when CLI behavior changes"
```
Store durable project memory that survives sessions and is queryable later.

```bash
brain release
```
Run pre-release checks for stale docs, env-var drift, risky diffs, and reviewer questions.

## How syncing actually works

There is no time-based expiration. Sync freshness is event/change based:

- `npm run sync` always performs a full project scan of tracked files.
- For project files, it updates chunks/summaries in the local DB.
- For chat history, it computes a hash and skips chat reindex when chat content is unchanged.
- If chat changed, only changed sessions regenerate summaries; unchanged sessions are reused.
- It removes records for files that were deleted from the repo.
- Final state is stored in `.codex_brain/index_state.json`.

Meaning of "how long does it stay synced":
- It stays valid indefinitely until source files or ingested chat data change.
- If nothing changes, your index remains current for that snapshot.
- If you change files and watcher is not running, you must run `npm run sync` manually.

## Auto-sync trigger rules (`npm run watch` / `npm start`)

Watcher triggers sync when supported files are created/modified/moved:
- `.py`, `.js`, `.ts`, `.tsx`, `.md`, `.txt`, `.json`, `.yaml`, `.yml`, `.sh`

Watcher ignores:
- `.git`, `.codex_brain`, `.venv`, `venv`, `__pycache__`, `node_modules`, `.pytest_cache`, `dist`, `build`

Debounce:
- Waits about `1.5s` after the last change before running sync.
- You can set `watch.debounce_seconds` in `brain.toml` (or pass `--debounce`).
- The watcher also writes a compact intelligence payload into `.codex_brain/watch_status.json`:
  changed files, likely subsystem, possibly stale docs, and reviewer questions.

## `.brainignore` and `brain.toml`

`brain sync` and `brain watch` now read both files (if present):

- `.brainignore`: path patterns to skip (`*.log`, `generated/`, `docs/archive/**`, etc.)
- `brain.toml`: project settings such as `index.include_extensions`, `index.ignore_dirs`, `index.ignore_patterns`, and `watch.debounce_seconds`
- `BRAIN_CONFIG_FILE`: optional env var to point Brain at a non-default `brain.toml` file

Scaffold both with:

```bash
brain init
```

For full guided onboarding, use:

```bash
brain start
```

## Runtime controls (optional)

Defaults:
- Progress/status output: enabled.
- Sync throttle: `0ms` per file.
- Chat summary workers: `3`.

Examples:

```bash
BRAIN_SYNC_THROTTLE_MS=0 npm run sync
```
Fastest per-file mode (default).

```bash
BRAIN_CHAT_SUMMARY_CONCURRENCY=4 npm run sync
```
Increase chat summary parallelism (faster, but higher API load).

```bash
BRAIN_SYNC_PROGRESS=0 npm run sync
```
Disable progress/status output.

## Stop background processes

If watcher is running in foreground:
- Press `Ctrl+C`.

If started in background:

```bash
pkill -f watch_brain.py
```
