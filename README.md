# Sudo-ID

Terminal-first project memory sidecar for local code + Codex session context.

## What it does

- Indexes project files into a local ChromaDB at `.codex_brain`
- Ingests Codex chat history (sessions-first, history fallback)
- Stores concise summaries for fast retrieval and reduced prompt load
- Supports quick context queries, note capture, and auto-sync on file changes
- Adds lightweight repo intelligence for onboarding, task mapping, safe refactors, work summaries, durable decisions, and release checks

## Requirements

```bash
python3 --version
npm --version
```

## Install As A Reusable CLI App

After publishing this repo, others can install it as a command-line app.

From GitHub (recommended for users):

```bash
pipx install "git+https://github.com/<org>/<repo>.git"
```

From a local clone:

```bash
cd /path/to/repo
pipx install .
```

For cross-repo local development, use an editable install from this checkout instead of a copied snapshot:

```bash
cd /workspaces/Sudo-ID
brain upgrade --source /workspaces/Sudo-ID --editable
```

Then use in any project directory:

```bash
cd /path/to/any-project
brain start
```

Run `brain version` to confirm which code is active. An editable local install shows `Source repo: /workspaces/Sudo-ID`.

`brain start` is the guided first-run wizard. It walks you through init, health checks, sync, optional question, and optional watcher startup.
For full-screen terminal UI, run `brain tui` (or `brain start --tui`).
In the TUI, running actions show a live progress strip; press `x` to cancel a long-running command.

## Context Sidecar

This repo includes a local "brain" backed by ChromaDB:

- `sync_brain.py`: indexes project files into `.codex_brain`, ingests Codex chat data (auto: `~/.codex/sessions`, fallback: `~/.codex/history.jsonl`), and stores concise summaries
- `ask_brain.py`: retrieves summary context only by default (high context efficiency), prioritizing chat/decision summaries before file summaries
- `memorize.py`: stores a distilled session note (from stdin)
- `watch_brain.py`: watches files and auto-runs sync on changes
- `brain_workflows.py`: workflow automation, onboarding, mapping, release checks, and durable project memory helpers

### Fast start (headless, terminal-only)

```bash
npm run setup
npm start
```

- `npm run setup`: creates `.venv`, installs Python dependencies, and runs an initial sync
- if `.venv` install is blocked, scripts fall back to system `python3` when dependencies already exist
- `npm start`: runs sync and starts the watcher (auto-sync on save/edit)
- `brain ...` commands are the installable cross-project interface (via `pipx install .`)
- `brain start` is the easiest onboarding command when starting a new project

### 1. Install dependencies

Use the one-liner:

```bash
npm run setup
```

Manual equivalent:

```bash
pip install chromadb openai python-dotenv watchdog tomli
```

Optional (higher-quality semantic embeddings, requires model download/network):

```bash
pip install sentence-transformers
```

### 2. Add your API key in `.env`

Recommended command (safe prompt input):

```bash
npm run set-key
```

Manual equivalent:

```bash
OPENAI_API_KEY=your_key_here
```

`OPENAI_API_KEY` is optional, but recommended. When present, both sync and query generate stronger summaries.
Without it, the tool falls back to local heuristic summaries so automation still works.
You can tune OpenAI call timeout (seconds) with `BRAIN_OPENAI_TIMEOUT` (default `8`).
Sync now shows file-level progress plus stage/status updates by default; disable with `BRAIN_SYNC_PROGRESS=0`.
Indexing now defaults to `BRAIN_SYNC_THROTTLE_MS=0` (no per-file delay). Increase it only if you need to lower local load.
Chat summary generation supports parallel workers via `BRAIN_CHAT_SUMMARY_CONCURRENCY` (default `3`).
When chat changes, summary refresh is incremental: unchanged sessions reuse their existing summaries.
If Chroma enters a write-corrupted state, `sync_brain.py` now auto-recovers by rebuilding the active collection and retrying a full sync.

Embedding backend defaults to an offline local hasher so this works without Hugging Face access.
To use sentence-transformers instead:

```bash
export BRAIN_EMBED_PROVIDER=sentence-transformers
export BRAIN_ST_MODEL=all-MiniLM-L6-v2
```

### 3. Build the initial index

```bash
npm run sync
brain sync
```

### 3a. Initialize config and run diagnostics

```bash
brain init
brain doctor
```

Prefer this for new users instead:

```bash
brain start
```

`brain init` scaffolds:

- `.env`
- `.brainignore`
- `brain.toml`

What `.brainignore` is for:

- It is a project-level ignore file for Brain indexing/watching.
- Add patterns you do not want in memory, such as `generated/`, `*.log`, or `docs/archive/**`.
- It is applied by both `brain sync` and `brain watch`.
- Set `BRAIN_CONFIG_FILE` if you want Brain to load a non-default `brain.toml` path.

One-command onboarding options:

```bash
brain start
brain start --tui
brain tui
brain start --yes --no-watch
```

### 4. Query memory

```bash
npm run ask -- "What is the main goal of Sudo-ID?"
brain ask "What is the main goal of Sudo-ID?"
```

Workflow helpers:

```bash
brain guide
brain map "watcher status"
brain refactor "sync progress output"
brain summarize
brain handoff
brain pr
brain decision --kind rule --title "Docs first" --text "Update README and COMMANDS when CLI behavior changes"
brain release
```

Optional flags:

```bash
npm run ask -- --mode human "How does indexing work?"
npm run ask -- --mode codex "How does chat ingestion work?"
npm run ask -- --include-code "Show sync implementation details"
npm run ask -- --raw-only "Debug retrieval output"
```

Default behavior is tuned to preserve context window usage: summary records are retrieved first, and raw code chunks are excluded unless `--include-code` is passed.
Default output mode is now `human` for cleaner readability.
To disable chat-first summary ordering and use pure similarity ordering, set `BRAIN_CHAT_FIRST=0`.
To force history-only chat ingestion, set `BRAIN_CHAT_SOURCE=history`.
To force sessions-based ingestion, set `BRAIN_CHAT_SOURCE=sessions` (tune `BRAIN_CHAT_MAX_SESSION_FILES` and `BRAIN_CHAT_MAX_SESSION_ENTRIES` if needed).
To override the default config file location, set `BRAIN_CONFIG_FILE=/path/to/brain.toml`.

### 5. Save session memory notes

Paste session notes into stdin:

```bash
npm run remember
brain remember --text "Decision: keep summaries concise"
```

Then paste text and press `Ctrl+D`.

### 6. Enable auto-sync on file changes

Foreground:

```bash
npm run watch
brain watch
```

Every detected save/edit triggers `sync_brain.py`, which updates code chunks and refreshes per-file summaries.
Default debounce is configurable via `brain.toml` (`watch.debounce_seconds`) or `brain watch --debounce`.
Watcher status now also records a compact change summary, likely affected subsystem, potentially stale docs, and reviewer questions in `.codex_brain/watch_status.json`.

Background:

```bash
nohup npm run watch > /tmp/watch_brain.log 2>&1 &
```

Stop watcher:

```bash
pkill -f watch_brain.py
```

### Optional shell shortcuts (Bash)

```bash
echo "alias brain='python3 /workspaces/Sudo-ID/ask_brain.py'" >> ~/.bashrc
echo "alias remember='python3 /workspaces/Sudo-ID/memorize.py'" >> ~/.bashrc
echo "alias brain-sync='python3 /workspaces/Sudo-ID/sync_brain.py'" >> ~/.bashrc
source ~/.bashrc
```
