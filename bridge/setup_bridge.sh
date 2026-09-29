#!/usr/bin/env bash
# Local Anthropic ↔ OpenAI gateway for CommandCode / Cline Pass (127.0.0.1:4000)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATEWAY_PY="$SCRIPT_DIR/gateway.py"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_NAME="ccp-gateway.service"

chmod +x "$GATEWAY_PY"

# :4000 只允许一个桥。停掉本机可能还在跑的 LiteLLM。
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user stop litellm-cmdc.service 2>/dev/null || true
  systemctl --user disable litellm-cmdc.service 2>/dev/null || true
fi
pkill -f 'litellm --config' 2>/dev/null || true

mkdir -p "$SERVICE_DIR"
cat > "$SERVICE_DIR/$SERVICE_NAME" <<EOF
[Unit]
Description=CCP Anthropic-to-OpenAI gateway (CommandCode + Cline Pass)
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 $GATEWAY_PY
Restart=on-failure
RestartSec=2
Environment=CCP_GATEWAY_PORT=4000
Environment=CCP_SECRETS_ENV=$HOME/.ccp/secrets.env

[Install]
WantedBy=default.target
EOF

if command -v systemctl >/dev/null 2>&1 && systemctl --user daemon-reload 2>/dev/null; then
  systemctl --user enable --now "$SERVICE_NAME"
  echo "✔ ccp-gateway.service listening on 127.0.0.1:4000"
else
  nohup python3 "$GATEWAY_PY" >/tmp/ccp-gateway.log 2>&1 &
  echo "✔ gateway started via nohup (systemd --user unavailable). log: /tmp/ccp-gateway.log"
fi

echo "Keys: CommandCode ~/.commandcode/auth.json ; Cline CLINE_API_KEY in ~/.ccp/secrets.env"
echo "Cline upstream model must keep cline-pass/ prefix (subscription vs pay-as-you-go)."
