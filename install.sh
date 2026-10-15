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
# A symlink, not a copy: a copy silently goes stale, and the user keeps running
# yesterday's version while the repo has moved on. The symptom is subtle —
# `ccp` listing a different set of providers than the repo defines, and new
# subcommands quietly doing nothing. `bin/ccp` resolves symlinks when locating
# the repo it belongs to, so a link always drives the current checkout.
mkdir -p "$BIN_DIR"
if [[ -e "$BIN_DIR/ccp" && ! -L "$BIN_DIR/ccp" ]]; then
  if cmp -s "$SCRIPT_DIR/bin/ccp" "$BIN_DIR/ccp"; then
    ln -sf "$SCRIPT_DIR/bin/ccp" "$BIN_DIR/ccp"
  else
    cp "$BIN_DIR/ccp" "$BIN_DIR/ccp.outdated-$(date +%Y%m%d-%H%M%S).bak"
    echo "  (previous ~/.local/bin/ccp differed from the repo; kept as .outdated-*.bak)"
  fi
fi
ln -sf "$SCRIPT_DIR/bin/ccp" "$BIN_DIR/ccp"
echo "✔ Linked 'ccp' -> $SCRIPT_DIR/bin/ccp"

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
for obsolete in cmdc-space-bunny.json longcat.json u2flash.json unisound.json openrouter.json minmax.json; do
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
