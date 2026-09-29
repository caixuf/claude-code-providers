#!/usr/bin/env bash
# install.sh — ccp (Claude Code Provider Switcher)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
CLAUDE_PROVIDERS_DIR="$HOME/.claude/providers"

echo "=========================================================="
echo "    Installing ccp (Claude Code Provider Switcher)       "
echo "=========================================================="

mkdir -p "$BIN_DIR"
ln -sfn "$SCRIPT_DIR/bin/ccp" "$BIN_DIR/ccp"
chmod +x "$SCRIPT_DIR/bin/ccp" "$SCRIPT_DIR/bridge/gateway.py"
echo "✔ ccp → $BIN_DIR/ccp (symlink to $SCRIPT_DIR/bin/ccp)"

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  echo "Notice: add  export PATH=\"\$HOME/.local/bin:\$PATH\"  to ~/.bashrc"
fi

mkdir -p "$CLAUDE_PROVIDERS_DIR"
copied_count=0
for example in "$SCRIPT_DIR/providers"/*.json.example; do
  [ -f "$example" ] || continue
  base=$(basename "$example" .example)
  target="$CLAUDE_PROVIDERS_DIR/$base"
  if [ ! -f "$target" ]; then
    cp "$example" "$target"
    copied_count=$((copied_count + 1))
  fi
done
echo "✔ ~/.claude/providers (added $copied_count templates; existing files kept)"

if [[ "${1:-}" == "--bridge" ]] || [[ "${1:-}" == "--with-bridge" ]]; then
  bash "$SCRIPT_DIR/bridge/setup_bridge.sh"
else
  echo "Tip: cmdc / cline need the local bridge:  bash $SCRIPT_DIR/bridge/setup_bridge.sh"
  echo "     or:  bash install.sh --bridge"
fi

echo ""
echo "  ccp                 # list"
echo "  ccp cmdc-deepseek   # switch (alias: ccp cmdc)"
echo "  ccp --current"
echo "=========================================================="
