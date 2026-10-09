# 🚀 ccp (Claude Code Provider Switcher)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform: Linux / macOS / WSL](https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20WSL-green.svg)]()
[![Tested on: Claude Code](https://img.shields.io/badge/Claude%20Code-Compatible-purple.svg)]()

> **Seamlessly switch LLM providers & models in Anthropic Claude Code CLI (`claude`).**  
> Supports both native Anthropic-compatible endpoints and OpenAI/OpenRouter-compatible providers (DeepSeek, CommandCode, ClinePass, MiniMax, Unisound, etc.).

---

## ✨ Features

- ⚡ **One-command switching**: Switch models in 1 second (`ccp cmdc-deepseek`, `ccp minimax`, `ccp deepseek`).
- 🛡️ **Safe & non-destructive**: Automatically creates timestamped backups of `~/.claude/settings.json` upon every switch; supports instant rollback with `ccp --rollback`.
- 🌉 **Universal Protocol Bridge**: Claude Code hardcodes the Anthropic Messages API (`/v1/messages`). `ccp` includes an optional zero-latency local proxy bridge (powered by LiteLLM) that allows Claude Code to access **any OpenAI-compatible gateway** (CommandCode, ClinePass, self-hosted vLLM) with full streaming, tool use, and thinking tokens!
- 🎯 **Preserves Full Capabilities**:
  - ✅ Tool calling (File search, editing, bash execution, LSP)
  - ✅ Thinking tokens (Extended reasoning / CoT streaming)
  - ✅ Stream-json / verbose output
- 🧪 **Battle-Tested**: Validated by rigorous automated 6-axis code generation benchmarks on complex real-world codebases.

---

## 📦 Quick Start

### 1. Installation

```bash
git clone https://github.com/caixuf/claude-code-providers.git ~/.claude-code-providers
cd ~/.claude-code-providers
bash install.sh
```

*(Ensure `~/.local/bin` is in your `PATH`)*

### 2. Usage

```bash
# List all available providers (* marks active)
ccp

# Switch to a provider profile
ccp cmdc-deepseek
ccp minimax
ccp deepseek

# Check current active profile and endpoints
ccp --current

# List models with auto-discovered context / output cap / vision
ccp models            # curated view
ccp models cmdc       # every model a provider exposes

# Refresh model metadata from each provider's official API, then regenerate
# config + profiles from it (single source of truth)
ccp discover
ccp render

# Sync local profiles with repository templates (archives obsolete ones, adds new
# templates, and reconciles derived context values without touching your keys)
ccp sync

# Quick ping test to verify the provider works
ccp --test

# Roll back to the previous settings backup
ccp --rollback
```

> **No more hand-filling context/multimodal.** Model capabilities are pulled
> straight from each provider's `/models` endpoint and LiteLLM's built-in model
> registry, so adding a model never means hand-typing its context window.

---

## 🧭 Model Registry (single source of truth)

Every fact about a model — its real context window, output cap, and whether it
accepts images — lives in **one place**, `registry/models.yaml`, and is
**auto-discovered**, not hand-written:

```bash
ccp discover        # pull context/vision/output from each provider's /models API
ccp render          # regenerate config.yaml, clamp_map.json, and profiles from it
```

Discovery merges three sources, in priority order:

1. **The provider's own `/models` endpoint** (authoritative):
   - CommandCode → `context_length`, `supported_endpoints` (~87 models)
   - Cline → id/name only
2. **LiteLLM's bundled model registry** (`litellm.model_cost`, ~4500 entries) for
   `max_input_tokens` / `max_output_tokens` / `supports_vision`.
3. A tiny defaults table for the few facts neither knows (e.g. every CommandCode
   model caps its **output** at 393216 — verified by probe).

`registry/profiles.yaml` is the *only* hand-authored file: it records choices
(which model each Claude Code tier maps to, and the short aliases). `ccp render`
joins the two into:

| Output | Purpose |
| :--- | :--- |
| `~/.config/litellm/config.yaml` | LiteLLM routing + `model_info` limits |
| `bridge/clamp_map.json` | model → output cap, used by the proxy |
| `providers/<name>.json.example` | profile with the *correct* `CLAUDE_CODE_MAX_CONTEXT_TOKENS` |

### Why this fixes the `max_tokens` 400

Claude Code sends `max_tokens` equal to the advertised context window (1,000,000
for a 1M model), but OpenAI-compatible gateways like CommandCode hard-cap output
at **393216**. The local bridge (`bridge/sse_watchdog.py`) now clamps
`max_tokens` down to the model's discovered output cap before forwarding — so a
1M-context model works end-to-end instead of erroring with
`400 Invalid max_tokens value`.

### Multimodal

Vision-capable models are declared automatically (`supports_vision`), and a
dedicated `cmdc-vision` profile maps every tier to a vision model, so pasting an
image just works:

```bash
ccp cmdc-vision     # all tiers -> deepseek/deepseek-v4-flash-vision-exp
```

---

## 🗂️ Provider Profiles

Provider profiles live in `~/.claude/providers/<name>.json`. 

Example configuration template:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
    "ANTHROPIC_AUTH_TOKEN": "your_api_key_here",
    "ANTHROPIC_MODEL": "deepseek-v4-flash",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-v4-flash",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-v4-flash",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
    "ANTHROPIC_SMALL_FAST_MODEL": "deepseek-v4-flash",
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000"
  },
  "permissions": { "defaultMode": "bypassPermissions" },
  "skipDangerousModePermissionPrompt": true,
  "theme": "dark"
}
```

Pre-configured templates in `providers/`:
- **`copilot.json`**: GitHub Copilot (Claude Sonnet 5.5, Haiku 5.5, Opus 5.5 tiers mapped) via local bridge
- **`cmdc-deepseek-fast.json`**: CommandCode DeepSeek v4.1 Flash Fast (high-throughput low-latency) via local bridge
- **`cmdc-deepseek-pro.json`**: CommandCode DeepSeek v4 Pro (hybrid-attention long-context reasoning) via local bridge
- **`cmdc-deepseek.json`**: CommandCode DeepSeek v4.1 Flash via local bridge
- **`cline-deepseek.json`**: ClinePass Subscription DeepSeek via local bridge
- **`minimax.json`**: MiniMax M3 (`api.minimaxi.com/anthropic`)
- **`deepseek.json`**: Official DeepSeek Anthropic API (`api.deepseek.com/anthropic`)

The three `cmdc-*` profiles are **three entry points onto one model ladder**, not
three unrelated models. Each pins Claude Code's four tiers to whichever rung
suits that entry point, so `ccp -m` can move within a provider and
`ccp <provider>` can move along the ladder:

| Profile | default | opus | sonnet | haiku | context |
| :--- | :--- | :--- | :--- | :--- | ---: |
| `cmdc-deepseek-fast` | `cmdc-deepseek-fast` | `cmdc-deepseek-pro` | `deepseek-v4.1-flash` | `cmdc-deepseek-fast` | **1M** |
| `cmdc-deepseek` | `deepseek-v4.1-flash` | `cmdc-deepseek-pro` | `deepseek-v4.1-flash` | `cmdc-deepseek-fast` | **1M** |
| `cmdc-deepseek-pro` | `cmdc-deepseek-pro` | `cmdc-deepseek-pro` | `deepseek-v4.1-flash` | `cmdc-deepseek-fast` | **1M** |

---

## 🌉 Universal Bridge (LiteLLM Local Gateway)

Claude Code strictly issues requests in Anthropic Messages format (`/v1/messages`). Many providers (e.g. CommandCode, ClinePass, self-hosted vLLM) only provide OpenAI `/chat/completions`.

`ccp` includes an automated local protocol bridge running on `127.0.0.1:4000`:

```
┌─────────────────────────────────┐
│     Claude Code CLI (claude)    │
└────────────────┬────────────────┘
                 │ Anthropic /v1/messages (Streaming + Tool use)
                 ▼
┌─────────────────────────────────┐
│  Local Bridge (127.0.0.1:4000)  │  <-- Zero-buffer loopback proxy (<0.2ms overhead)
└───────┬─────────────────┬───────┘
        │                 │
        ▼                 ▼ OpenAI /chat/completions
┌────────────────┐ ┌────────────────┐
│  CommandCode   │ │   ClinePass    │
│  (DeepSeek)    │ │ (Token Plan)   │
└────────────────┘ └────────────────┘
```

### Enabling the Bridge

Run the automated bridge installer:
```bash
bash bridge/setup_bridge.sh
```

This will:
1. Create an isolated virtualenv at `~/.local/share/litellm-venv`.
2. Apply critical upstream gateway patches:
   - **ClinePass token plan compatibility**: Uses `cline-pass/` prefix to correctly draw from flat subscriptions instead of pay-as-you-go balance.
   - **Gateway unwrap patch**: Unwraps non-standard `{"data": {"choices": ...}}` response envelopes.
   - **User header filtering**: Drops custom client headers that cause `400 Bad Request` on strict proxies.
3. Register a `systemd --user` service for LiteLLM on `127.0.0.1:4000`.
   Pass `--no-watchdog` for the single-process shape; without it the installer also adds
   `ccp-sse-watchdog.service` on `:4000` in front of LiteLLM on `:4001`. See below.

### Deployment modes

Every provider profile connects to `127.0.0.1:4000`. What sits behind that port
is your choice — both shapes below are supported and self-consistent.

**Single process (recommended)** — LiteLLM owns `:4000` directly:

    claude ──> :4000 LiteLLM ──> upstream

```bash
bash bridge/setup_bridge.sh --no-watchdog
```

One process, one hop, nothing to keep in sync. Re-running it also removes a
`ccp-sse-watchdog.service` left behind by an earlier watchdog install, which
would otherwise fight LiteLLM for the port.

**With watchdog** — LiteLLM on `:4001`, `sse_watchdog` on `:4000` in front:

    claude ──> :4000 sse_watchdog ──> :4001 LiteLLM ──> upstream

```bash
bash bridge/setup_bridge.sh          # default
```

The watchdog closes the Anthropic SSE stream after a complete `message_stop`
and drops trailing keepalives. Reach for it only if your upstream leaves streams
open after a completed message — otherwise it is an extra hop for no benefit.

> **History worth knowing.** An earlier idle-injection bug in the watchdog
> spliced synthetic JSON into a live stream. LiteLLM rejected that as
> `MidStreamFallbackError` and Claude Code then retried for roughly ten minutes.
> That path is gone — idle injection is disabled by default and the injector was
> removed — but it is the reason single-process is the mode to reach for first.

Check which one you are on:

```bash
systemctl --user status litellm-cmdc ccp-sse-watchdog
ss -ltnp | grep 4000
```

If `:4000` belongs to `litellm` you are on single-process; if it belongs to
`sse_watchdog.py`, LiteLLM is behind it on `:4001`.

---

## 🐙 Using GitHub Copilot with Claude Code

You can use your active **GitHub Copilot** subscription as the backend provider for Claude Code.

### 1. Authenticate GitHub Copilot
Run the one-click device authorization flow:
```bash
ccp auth copilot
```
Follow the prompt to visit `https://github.com/login/device` and enter the code. Once authorized, Copilot tokens are saved to `~/.config/litellm/github_copilot/`.

### 2. Restart Bridge & Switch Profile
```bash
# Restart bridge to load credentials
ccp bridge restart

# Switch to GitHub Copilot
ccp copilot
```

### 3. Model Tiers (Claude 5.5 Official Family)
Copilot profile perfectly maps Claude Code's model hierarchy:
- **Default / Active / Sonnet**: `claude-sonnet-5.5` (Claude Sonnet 5.5 — 1M context)
- **Fast / Background / Haiku**: `claude-haiku-5.5` (Claude Haiku 5.5 — ultra-fast subagents & compact)
- **Heavy / Architecture / Opus**: `claude-opus-5.5` (Claude Opus 5.5 — maximum reasoning power)

---

## 📊 Benchmark & Performance Insights

In automated multi-turn engineering benchmarks across complex automotive codebases, all supported models achieved stellar performance under Claude Code:

| Provider | Model | Quality Score | Generation Speed (tok/s) | Highlights |
| :--- | :--- | :---: | :---: | :--- |
| **`claude-deepseek`** | `deepseek-v4-flash` | **12.0 / 12** | **213.6 tok/s** | Blazing-fast raw generation speed |
| **`claude-cmdc-deepseek`** | `deepseek-v4.1-flash` | **11.8 / 12** | **58.4 tok/s** | Best overall consistency in deep multi-turn sessions |
| **`claude-cline-deepseek`** | `deepseek-v4.1-flash` | **12.0 / 12** | **69.8 tok/s** | High speed, flat monthly quota |
| **`claude-minimax-m3`** | `MiniMax-M3` | **10.8 / 12** | **58.9 tok/s** | Lowest latency, concise and direct answers |

---

## 📄 License

MIT License. Feel free to use, modify, and distribute!
