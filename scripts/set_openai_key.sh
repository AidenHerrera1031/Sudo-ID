#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f ".env" ]]; then
  touch .env
fi

read -r -s -p "Paste OPENAI_API_KEY: " OPENAI_KEY
echo

if [[ -z "${OPENAI_KEY:-}" ]]; then
  echo "No key entered. Nothing changed." >&2
  exit 1
fi

TMP_FILE="$(mktemp)"
grep -v '^OPENAI_API_KEY=' .env > "$TMP_FILE" || true
printf 'OPENAI_API_KEY=%s\n' "$OPENAI_KEY" >> "$TMP_FILE"
mv "$TMP_FILE" .env
chmod 600 .env || true

echo "OPENAI_API_KEY saved to .env."
