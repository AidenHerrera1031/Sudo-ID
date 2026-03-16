#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

has_brain_deps() {
  local py_bin="$1"
  "$py_bin" -c "import chromadb, dotenv, openai" >/dev/null 2>&1
}

SKIP_SYNC=0
for arg in "$@"; do
  case "$arg" in
    --skip-sync)
      SKIP_SYNC=1
      ;;
    *)
      echo "Unknown option: $arg" >&2
      echo "Usage: bash ./scripts/setup.sh [--skip-sync]" >&2
      exit 1
      ;;
  esac
done

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required but not installed." >&2
  exit 1
fi

if [[ ! -d ".venv" ]]; then
  echo "Creating Python virtual environment (.venv)..."
  python3 -m venv .venv
fi

PYTHON_BIN=".venv/bin/python"
if has_brain_deps "$PYTHON_BIN"; then
  echo "Python dependencies already available in .venv."
elif has_brain_deps python3; then
  echo "System Python already has required packages. Using fallback runtime."
else
  echo "Installing Python dependencies into .venv..."
  if ! "$PYTHON_BIN" -m pip install --upgrade pip >/dev/null 2>&1; then
    echo "Warning: could not upgrade pip in .venv (continuing)."
  fi
  if ! "$PYTHON_BIN" -m pip install -r requirements.txt; then
    echo "Warning: .venv dependency install failed."
    echo "No working runtime found. Please ensure internet access and rerun: npm run setup" >&2
    exit 1
  fi
fi

if [[ ! -f ".env" ]]; then
  touch .env
fi

if ! grep -q '^OPENAI_API_KEY=' .env; then
  if [[ -n "${OPENAI_API_KEY:-}" ]]; then
    printf 'OPENAI_API_KEY=%s\n' "$OPENAI_API_KEY" >> .env
    echo "Saved OPENAI_API_KEY from shell environment into .env."
  else
    echo "OPENAI_API_KEY is not set yet. Run: npm run set-key"
  fi
fi

if [[ "$SKIP_SYNC" -eq 0 ]]; then
  echo "Running initial memory sync..."
  if has_brain_deps "$PYTHON_BIN"; then
    "$PYTHON_BIN" sync_brain.py
  else
    python3 sync_brain.py
  fi
fi

echo "Setup complete."
