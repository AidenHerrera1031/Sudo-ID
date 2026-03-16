#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

MODE="${1:-watch}"

case "$MODE" in
  watch)
    bash ./scripts/setup.sh --skip-sync

    echo "Syncing project and chat memory..."
    bash ./scripts/with_python.sh sync_brain.py

    echo "Starting watcher (Ctrl+C to stop)..."
    exec bash ./scripts/with_python.sh watch_brain.py
    ;;
  tui)
    bash ./scripts/setup.sh --skip-sync

    echo "Starting Brain TUI..."
    exec bash ./scripts/with_python.sh brain_cli.py tui
    ;;
  *)
    echo "Unknown start mode: $MODE" >&2
    echo "Usage: bash ./scripts/start.sh [watch|tui]" >&2
    exit 1
    ;;
esac
