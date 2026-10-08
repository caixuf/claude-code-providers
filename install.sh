#!/usr/bin/env bash
# install.sh — One-click installer for ccp (Claude Code Provider Switcher)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
CLAUDE_PROVIDERS_DIR="$HOME/.claude/providers"

echo "=========================================================="
echo "    Installing ccp (Claude Code Provider Switcher)       "
echo "=========================================================="

# 1. Install CLI binary
mkdir -p "$BIN_DIR"
cp "$SCRIPT_DIR/bin/ccp" "$BIN_DIR/ccp"
chmod +x "$BIN_DIR/ccp"
echo "✔ Installed 'ccp' binary to $BIN_DIR/ccp"

# 2. Check PATH
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  echo ""
  echo "Notice: $BIN_DIR is not in your current PATH."
  echo "Add the following line to your ~/.bashrc or ~/.zshrc:"
  echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
  echo ""
fi

# 3. Initialize ~/.claude/providers directory with example profiles
mkdir -p "$CLAUDE_PROVIDERS_DIR"
mkdir -p "$HOME/.claude/providers_disabled"

# Archive obsolete / discontinued profiles
for obsolete in cmdc-space-bunny.json longcat.json u2flash.json; do
  if [ -f "$CLAUDE_PROVIDERS_DIR/$obsolete" ]; then
    mv "$CLAUDE_PROVIDERS_DIR/$obsolete" "$HOME/.claude/providers_disabled/"
    echo "  -- Archived deprecated profile $obsolete to ~/.claude/providers_disabled/"
  fi
done

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
echo "✔ Initialized profiles in $CLAUDE_PROVIDERS_DIR (added $copied_count templates)"

# 4. Optional LiteLLM Bridge setup
if [[ "${1:-}" == "--bridge" ]]; then
  echo ""
  echo "Setting up local LiteLLM proxy bridge..."
  bash "$SCRIPT_DIR/bridge/setup_bridge.sh"
else
  echo ""
  echo "Tip: To enable OpenAI / CommandCode / ClinePass models,"
  echo "run the local bridge setup: bash $SCRIPT_DIR/bridge/setup_bridge.sh"
fi

echo ""
echo "Installation complete! Try running:"
echo "  ccp              # List all available providers"
echo "  ccp <name>       # Switch provider"
echo "  ccp --current    # Check active provider"
echo "=========================================================="
