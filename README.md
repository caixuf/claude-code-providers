# 🚀 ccp (Claude Code Provider Switcher)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform: Linux / macOS / WSL](https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20WSL-green.svg)]()
[![Tested on: Claude Code](https://img.shields.io/badge/Claude%20Code-Compatible-purple.svg)]()

> **Seamlessly switch LLM providers & models in Anthropic Claude Code CLI (`claude`).**  
> Supports both native Anthropic-compatible endpoints and OpenAI/OpenRouter-compatible providers (DeepSeek, CommandCode, ClinePass, MiniMax, StepFun, Unisound, etc.).

---

## ✨ Features

- ⚡ **One-command switching**: Switch models in 1 second (`ccp cmdc-deepseek`, `ccp minmax`, `ccp deepseek`).
- 🛡️ **Safe & non-destructive**: Automatically creates timestamped backups of `~/.claude/settings.json` upon every switch; supports instant rollback with `ccp --rollback`.
- 🌉 **Universal Protocol Bridge**: Claude Code hardcodes the Anthropic Messages API (`/v1/messages`). `ccp` includes an optional zero-latency local proxy bridge (powered by LiteLLM) that allows Claude Code to access **any OpenAI-compatible gateway** (CommandCode, ClinePass, OpenRouter) with full streaming, tool use, and thinking tokens!
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
ccp minmax
ccp deepseek

# Check current active profile and endpoints
ccp --current

# Quick ping test to verify the provider works
ccp --test

# Roll back to the previous settings backup
ccp --rollback
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
- **`deepseek.json`**: Official DeepSeek Anthropic API (`api.deepseek.com/anthropic`)
- **`minimax.json`**: MiniMax M3 (`api.minimaxi.com/anthropic`)
- **`stepfun.json`**: StepFun Step-5 / 3.7 (`api.stepfun.com/step_plan`)
- **`longcat.json`**: LongCat 2.5 Preview (`api.longcat.chat/anthropic`)
- **`unisound.json`**: Unisound u2-flash (`maas-api.unisound.com/anthropic`)
- **`cmdc-deepseek.json`**: CommandCode DeepSeek via local bridge
- **`cmdc-deepseek-fast.json`**: CommandCode DeepSeek v4.1 Flash Fast (high-throughput low-latency) via local bridge
- **`cmdc-space-bunny.json`**: CommandCode Space Bunny (`stealth/space-bunny-alpha`, 1M context) via local bridge
- **`cline-deepseek.json`**: ClinePass Subscription DeepSeek via local bridge

---

## 🌉 Universal Bridge (LiteLLM Local Gateway)

Claude Code strictly issues requests in Anthropic Messages format (`/v1/messages`). Many providers (e.g. CommandCode, ClinePass, OpenRouter, self-hosted vLLM) only provide OpenAI `/chat/completions`.

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
3. Register `systemd --user` services: LiteLLM on `127.0.0.1:4001`, plus `ccp-sse-watchdog.service` on `127.0.0.1:4000`. The watchdog drops keepalive frames after a complete `message_stop`. It does **not** inject JSON into a live stream (that triggered LiteLLM `MidStreamFallbackError` and 10-minute Claude retries).

---

## 📊 Benchmark & Performance Insights

In automated multi-turn engineering benchmarks across complex automotive codebases, all supported models achieved stellar performance under Claude Code:

| Provider | Model | Quality Score | Generation Speed (tok/s) | Highlights |
| :--- | :--- | :---: | :---: | :--- |
| **`claude-deepseek`** | `deepseek-v4-flash` | **12.0 / 12** | **213.6 tok/s** | Blazing-fast raw generation speed |
| **`claude-cmdc-deepseek`** | `deepseek-v4.1-flash` | **11.8 / 12** | **58.4 tok/s** | Best overall consistency in deep multi-turn sessions |
| **`claude-cline-deepseek`** | `deepseek-v4.1-flash` | **12.0 / 12** | **69.8 tok/s** | High speed, flat monthly quota |
| **`claude-minmax-m3`** | `MiniMax-M3` | **10.8 / 12** | **58.9 tok/s** | Lowest latency, concise and direct answers |
| **`claude-step5`** | `step-5-preview` | **11.8 / 12** | **56.6 tok/s** | Exceptionally detailed step-by-step reasoning |

---

## 📄 License

MIT License. Feel free to use, modify, and distribute!
