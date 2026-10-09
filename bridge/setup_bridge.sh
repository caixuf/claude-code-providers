#!/usr/bin/env bash
# setup_bridge.sh — Set up the local LiteLLM proxy bridge for Claude Code
# Converts Anthropic /v1/messages calls to OpenAI/OpenRouter /chat/completions

set -e

# ── Deployment mode ───────────────────────────────────────────────
# Provider profiles always connect to 127.0.0.1:4000. What sits behind
# that port is chosen here:
#
#   --no-watchdog   LiteLLM owns :4000 directly. One process, one hop,
#                  no moving parts. This is the default recommendation.
#   (no flag)       LiteLLM on :4001 with sse_watchdog on :4000 in front.
#                  The watchdog closes the Anthropic SSE stream after a
#                  complete message_stop and drops trailing keepalives --
#                  only needed if your upstream leaves streams open.
WITH_WATCHDOG=1
usage() {
  sed -n '2,20p' "$0" | sed 's/^# \?//'
  exit 0
}
for arg in "$@"; do
  case "$arg" in
    --no-watchdog) WITH_WATCHDOG=0 ;;
    -h|--help)     usage ;;
    *) echo "Unknown option: $arg" >&2; usage >&2 ;;
  esac
done

if [ "$WITH_WATCHDOG" = 1 ]; then
  LITELLM_PORT=4001          # watchdog listens on 4000 in front of it
  echo "Bridge mode: watchdog (LiteLLM :$LITELLM_PORT -> sse_watchdog :4000)"
else
  LITELLM_PORT=4000          # LiteLLM takes the Claude Code port directly
  echo "Bridge mode: single process (LiteLLM :$LITELLM_PORT)"
fi

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

# Patch C: GitHub Copilot non-interactive daemon safety patch
AUTH_FILE=$("$VENV_DIR/bin/python" -c "import litellm.llms.github_copilot.authenticator as m; print(m.__file__)" 2>/dev/null || true)
if [ -f "$AUTH_FILE" ]; then
  echo "Checking Copilot non-interactive daemon patch in $AUTH_FILE..."
  "$VENV_DIR/bin/python" - <<PY
with open("$AUTH_FILE", "r") as f:
    content = f.read()

target = '        for attempt in range(3):'
guard = '''        import sys
        if not sys.stdin.isatty() and not os.getenv("GITHUB_COPILOT_FORCE_LOGIN"):
            raise GetAccessTokenError(message="No existing access token found. Run 'ccp auth copilot' to authenticate.", status_code=401)
        for attempt in range(3):'''

if 'GITHUB_COPILOT_FORCE_LOGIN' not in content and target in content:
    content = content.replace(target, guard, 1)
    with open("$AUTH_FILE", "w") as f:
        f.write(content)
    print("✔ Copilot non-interactive daemon patch applied successfully.")
else:
    print("✔ Copilot non-interactive daemon patch already in place or not needed.")
PY
fi

echo "=== 4. Setting up Configuration ==="
mkdir -p "$CONFIG_DIR"

# Migrate any literal api_key values from an existing config.yaml into the
# secrets env file, so the generated config (which references os.environ/*)
# keeps working for people upgrading from the old hand-edited config.
migrate_keys_to_env() {
  local src="$1" dst="$2"
  [ -f "$src" ] || return 0
  "$VENV_DIR/bin/python3" - "$src" "$dst" <<'PY'
import re, sys, pathlib
src, dst = sys.argv[1], sys.argv[2]
text = pathlib.Path(src).read_text()
# map api_base host -> env var name
HOSTS = {
    "api.commandcode.ai": "CMDC_API_KEY",
    "api.cline.bot": "CLINE_API_KEY",
    "api.stepfun.com": "STEPFUN_API_KEY",
}
lines, cur_host = [], None
out = {}
for ln in text.splitlines():
    for host, var in HOSTS.items():
        if host in ln:
            cur_host = var
    m = re.match(r"\s*api_key:\s*(\S+)\s*$", ln)
    if m and cur_host:
        val = m.group(1).strip("'\"")
        if not val.startswith("os.environ/") and "YOUR_" not in val:
            out.setdefault(cur_host, val)
        cur_host = None
if not out:
    sys.exit(0)
existing = ""
dp = pathlib.Path(dst)
if dp.is_file():
    existing = dp.read_text()
add = [f"{k}={v}" for k, v in out.items() if f"{k}=" not in existing]
if add:
    with dp.open("a") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write("\n".join(add) + "\n")
    print(f"  ✔ Migrated {len(add)} API key(s) into {dst}")
PY
}

# Generate config from the registry (single source of truth) when available,
# else fall back to the checked-in example.
REGISTRY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/registry"
if [ -f "$REGISTRY_DIR/models.yaml" ] && [ -f "$SCRIPT_DIR/render_config.py" ]; then
  "$VENV_DIR/bin/python3" "$SCRIPT_DIR/render_config.py" >/dev/null 2>&1 || true
fi
if [ -f "$SCRIPT_DIR/config.generated.yaml" ]; then
  migrate_keys_to_env "$CONFIG_DIR/config.yaml" "$CONFIG_DIR/proxy.env"
  if [ -f "$CONFIG_DIR/config.yaml" ]; then
    cp "$CONFIG_DIR/config.yaml" "$CONFIG_DIR/config.yaml.bak.$(date +%Y%m%d-%H%M%S)"
  fi
  cp "$SCRIPT_DIR/config.generated.yaml" "$CONFIG_DIR/config.yaml"
  # also ship the clamp map next to the watchdog
  echo "Generated $CONFIG_DIR/config.yaml from registry (keys via proxy.env)"
elif [ ! -f "$CONFIG_DIR/config.yaml" ]; then
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
ExecStart=$VENV_DIR/bin/litellm --config $CONFIG_DIR/config.yaml --port $LITELLM_PORT --host 127.0.0.1
Restart=always
RestartSec=3
TimeoutStopSec=3
Environment=PYTHONUNBUFFERED=1
Environment=LITELLM_LOCAL_MODEL_COST_MAP=True
Environment=LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES=true
EnvironmentFile=-%h/.config/litellm/proxy.env

[Install]
WantedBy=default.target
EOF

# Detect and write default proxy.env if active
PROXY_URL="${https_proxy:-${http_proxy:-${HTTPS_PROXY:-${HTTP_PROXY:-}}}}"
if [ -z "$PROXY_URL" ]; then
  for p in 7897 7899 7892; do
    if timeout 1 bash -c "cat < /dev/null > /dev/tcp/127.0.0.1/$p" 2>/dev/null; then
      PROXY_URL="http://127.0.0.1:$p"
      break
    fi
  done
fi
if [ -n "$PROXY_URL" ] && [ ! -f "$CONFIG_DIR/proxy.env" ]; then
  cat > "$CONFIG_DIR/proxy.env" <<EOF
http_proxy=$PROXY_URL
https_proxy=$PROXY_URL
HTTP_PROXY=$PROXY_URL
HTTPS_PROXY=$PROXY_URL
no_proxy=localhost,127.0.0.1,::1,.local
NO_PROXY=localhost,127.0.0.1,::1,.local
EOF
  echo "✔ Detected proxy and wrote $CONFIG_DIR/proxy.env"
fi

if [ "$WITH_WATCHDOG" = 0 ]; then
  if systemctl --user list-unit-files ccp-sse-watchdog.service >/dev/null 2>&1; then
    systemctl --user disable --now ccp-sse-watchdog.service 2>/dev/null || true
    rm -f "$SERVICE_DIR/ccp-sse-watchdog.service"
    echo "-- Removed leftover ccp-sse-watchdog.service"
  fi
  systemctl --user daemon-reload
  systemctl --user enable litellm-cmdc.service
  systemctl --user restart litellm-cmdc.service
  echo "✔ LiteLLM on 127.0.0.1:$LITELLM_PORT (single process, Claude Code target)"
else
  cat > "$SERVICE_DIR/ccp-sse-watchdog.service" <<EOF
[Unit]
Description=CCP SSE watchdog in front of LiteLLM (close Anthropic streams after message_stop)
After=network.target litellm-cmdc.service
Requires=litellm-cmdc.service

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
ExecStart=$VENV_DIR/bin/python3 $SCRIPT_DIR/sse_watchdog.py --host 127.0.0.1 --port 4000 --upstream http://127.0.0.1:4001 --idle-seconds 0
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

  systemctl --user daemon-reload
  systemctl --user enable litellm-cmdc.service ccp-sse-watchdog.service
  systemctl --user restart litellm-cmdc.service
  sleep 2
  systemctl --user restart ccp-sse-watchdog.service
  echo "✔ LiteLLM on 127.0.0.1:$LITELLM_PORT ; SSE watchdog on 127.0.0.1:4000 (Claude Code target)"
fi

echo ""
echo "=== Setup Complete! ==="
echo "The bridge is listening on http://127.0.0.1:4000"
echo "You can check status with: systemctl --user status litellm-cmdc"
