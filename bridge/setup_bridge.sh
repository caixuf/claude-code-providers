#!/usr/bin/env bash
# setup_bridge.sh — Set up the local LiteLLM proxy bridge for Claude Code
# Converts Anthropic /v1/messages calls to OpenAI/OpenRouter /chat/completions

set -e

VENV_DIR="$HOME/.local/share/litellm-venv"
CONFIG_DIR="$HOME/.config/litellm"
SERVICE_DIR="$HOME/.config/systemd/user"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== 1. Setting up Python Virtual Environment ==="
if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
  echo "Created virtualenv at $VENV_DIR"
fi

echo "=== 2. Installing LiteLLM Proxy and Dependencies ==="
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install 'litellm[proxy]' 'httpx[socks]' 'socksio'

echo "=== 3. Applying Compatibility Patches ==="
# Patch A: Support wrapped data.choices (e.g. Cline Pass non-streaming returns)
CONVERT_FILE=$("$VENV_DIR/bin/python" -c "import litellm.litellm_core_utils.llm_response_utils.convert_dict_to_response as m; print(m.__file__)")
if [ -f "$CONVERT_FILE" ]; then
  if ! grep -q 'response_object = response_object\["data"\]' "$CONVERT_FILE"; then
    echo "Applying patch for gateway response unwrapping to $CONVERT_FILE..."
    "$VENV_DIR/bin/python" - <<PY
with open("$CONVERT_FILE", "r") as f:
    content = f.read()

target = '    try:\n        if response_type == "completion"'
replacement = '    try:\n        if isinstance(response_object, dict) and isinstance(response_object.get("data"), dict) and "choices" in response_object["data"]:\n            response_object = response_object["data"]\n        if response_type == "completion"'

if target in content:
    content = content.replace(target, replacement, 1)
    with open("$CONVERT_FILE", "w") as f:
        f.write(content)
    print("✔ Response unwrap patch applied successfully.")
else:
    print("Notice: target anchor not matched, skipping unwrap patch.")
PY
  else
    echo "✔ Response unwrap patch already in place."
  fi
fi

# Patch B: Drop unknown user tracking parameters for strict gateways like CommandCode
TRANSFORM_FILE=$("$VENV_DIR/bin/python" -c "import litellm.llms.anthropic.experimental_pass_through.responses_adapters.transformation as m; print(m.__file__)" 2>/dev/null || true)
if [ -f "$TRANSFORM_FILE" ]; then
  echo "Checking user tracking patch in $TRANSFORM_FILE..."
  "$VENV_DIR/bin/python" - <<PY
with open("$TRANSFORM_FILE", "r") as f:
    content = f.read()

old_code = '            user_id = user_from_metadata(metadata["user_id"])\n            if user_id is not None:\n                responses_kwargs["user"] = user_id'
if old_code in content:
    content = content.replace(old_code, '            # user omitted to prevent 400 on strict upstream gateways\n            pass')
    with open("$TRANSFORM_FILE", "w") as f:
        f.write(content)
    print("✔ Upstream gateway user patch applied successfully.")
else:
    print("✔ Upstream gateway user patch already in place or not needed.")
PY
fi

echo "=== 4. Setting up Configuration ==="
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/config.yaml" ]; then
  cp "$SCRIPT_DIR/config.example.yaml" "$CONFIG_DIR/config.yaml"
  echo "Created $CONFIG_DIR/config.yaml from template. Please edit with your API keys!"
else
  echo "Found existing $CONFIG_DIR/config.yaml"
fi

echo "=== 5. Setting up Systemd User Service ==="
mkdir -p "$SERVICE_DIR"
cat > "$SERVICE_DIR/litellm-cmdc.service" <<EOF
[Unit]
Description=LiteLLM Proxy Bridge for Claude Code
After=network.target

[Service]
Type=simple
ExecStart=$VENV_DIR/bin/litellm --config $CONFIG_DIR/config.yaml --port 4000 --host 127.0.0.1
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1
Environment=LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES=true

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable litellm-cmdc.service
systemctl --user restart litellm-cmdc.service
echo "✔ LiteLLM user service enabled and started on 127.0.0.1:4000"

echo ""
echo "=== Setup Complete! ==="
echo "The bridge is listening on http://127.0.0.1:4000"
echo "You can check status with: systemctl --user status litellm-cmdc"
