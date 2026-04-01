#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ensure_env_gitignored() {
  local gitignore_path=".gitignore"

  if [[ ! -f "$gitignore_path" ]]; then
    printf '.env\n' > "$gitignore_path"
    return
  fi

  if grep -qxF '.env' "$gitignore_path"; then
    return
  fi

  if [[ -s "$gitignore_path" ]]; then
    printf '\n.env\n' >> "$gitignore_path"
  else
    printf '.env\n' >> "$gitignore_path"
  fi
}

if [[ -f ".env" ]]; then
  echo "Existing .env found."
  echo "Open this file in the editor pane and add your key:"
  echo "  $ROOT_DIR/.env"
  exit 0
fi

ensure_env_gitignored
cat > ".env" <<'EOF'
# Local secrets for Brain. This file should stay out of git.
OPENAI_API_KEY=

# Optional tuning
# BRAIN_OPENAI_TIMEOUT=8
# BRAIN_SYNC_PROGRESS=1
EOF
chmod 600 .env || true

echo "Created .env and added .env to .gitignore."
echo "Open this file in the editor pane and add your key:"
echo "  $ROOT_DIR/.env"
echo ""
echo "Add this line:"
echo "  OPENAI_API_KEY=your_key_here"
