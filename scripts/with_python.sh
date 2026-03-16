#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

has_brain_deps() {
  local py_bin="$1"
  "$py_bin" -c "import chromadb, dotenv, openai" >/dev/null 2>&1
}

if [[ $# -lt 1 ]]; then
  echo "Usage: bash ./scripts/with_python.sh <script.py> [args...]" >&2
  exit 1
fi

if [[ -x ".venv/bin/python" ]] && has_brain_deps ".venv/bin/python"; then
  exec ".venv/bin/python" "$@"
fi

if has_brain_deps "python3"; then
  exec "python3" "$@"
fi

echo "Missing Python dependencies. Running setup..."
bash ./scripts/setup.sh --skip-sync

if [[ -x ".venv/bin/python" ]] && has_brain_deps ".venv/bin/python"; then
  exec ".venv/bin/python" "$@"
fi
if has_brain_deps "python3"; then
  exec "python3" "$@"
fi

echo "Could not find a Python runtime with required packages." >&2
echo "Required: chromadb, openai, python-dotenv, watchdog" >&2
exit 1
