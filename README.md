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
- 🌉 **Universal Protocol Bridge**: Claude Code hardcodes the Anthropic Messages API (`/v1/messages`). `ccp` includes an optional zero-latency local proxy bridge (powered by LiteLLM) that allows Claude Code to access **any OpenAI-compatible gateway** (CommandCode, ClinePass, CodeBuddy, self-hosted vLLM) with full streaming, tool use, and thinking tokens!
- 🎯 **Preserves Full Capabilities**:
  - ✅ Tool calling (File search, editing, bash execution, LSP)
  - ✅ Thinking tokens (Extended reasoning / CoT streaming)
  - ✅ Stream-json / verbose output
- 🧪 **Battle-Tested**: Validated by rigorous automated 6-axis code generation benchmarks on complex real-world codebases.
- 🐧 **CodeBuddy subscriptions**: Turn a CodeBuddy (Tencent coding-copilot) plan into a Claude Code backend — the CLI's existing login is imported with `ccp auth codebuddy`, and the model fleet is discovered from your account.

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
ccp codebuddy          # after: ccp auth codebuddy

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
   - CodeBuddy → `maxInputTokens`, `maxOutputTokens`, `supportsImages`, from
     `GET /v2/enterprises/personal/models` (account-scoped, ~18–31 models)
   - Cline → id/name only
2. **LiteLLM's bundled model registry** (`litellm.model_cost`, ~4500 entries) for
   `max_input_tokens` / `max_output_tokens` / `supports_vision`.
3. A tiny defaults table for the few facts neither knows (e.g. every CommandCode
   model caps its **output** at 393216 — verified by probe, and the CodeBuddy ids
   the gateway routes but the INTL catalogue omits).

`registry/profiles.yaml` is the *only* hand-authored file: it records choices
(which model each Claude Code tier maps to, the short aliases, and which
per-provider request rewrites the proxy applies). `ccp render` joins the two into:

| Output | Purpose |
| :--- | :--- |
| `~/.config/litellm/config.yaml` | LiteLLM routing + `model_info` limits |
| `bridge/clamp_map.json` | model → output cap, used by the proxy |
| `bridge/request_rules.json` | model → request rewrites, used by the proxy |
| `providers/<name>.json.example` | profile with the *correct* `CLAUDE_CODE_MAX_CONTEXT_TOKENS` |

### Why this fixes the `max_tokens` 400

Claude Code sends `max_tokens` equal to the advertised context window (1,000,000
for a 1M model), but OpenAI-compatible gateways like CommandCode hard-cap output
at **393216**. The local bridge (`bridge/sse_watchdog.py`) now clamps
`max_tokens` down to the model's discovered output cap before forwarding — so a
1M-context model works end-to-end instead of erroring with
`400 Invalid max_tokens value`.

> This clamp lives **only in watchdog mode**. In the single-process shape nothing
> rewrites the request, so the `400` comes straight back. See
> [Deployment modes](#deployment-modes) before choosing a shape.

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
- **`codebuddy.json`**: CodeBuddy / 腾讯 coding-copilot subscription (DeepSeek v4.1 Flash, GLM-5.3, GLM-5.3-Flash) via local bridge
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

`codebuddy` is the same idea across *different vendors*, because no single one
covers all four tiers well. Every tier is 1M-context on purpose: Claude Code
advertises one window per session but compacts through the haiku tier, so a
smaller haiku model would break compaction on a long session.

| Profile | default | opus / fable | sonnet | haiku | context |
| :--- | :--- | :--- | :--- | :--- | ---: |
| `codebuddy` | `codebuddy` (DeepSeek v4.1 Flash, free) | `codebuddy-glm` (GLM-5.3) | `codebuddy` | `codebuddy-fast` (GLM-5.3-Flash) | **1M** |

Hot-swap to another vendor in the same subscription with `ccp -m`:

```bash
ccp -m codebuddy-glm       # GLM-5.3
ccp -m codebuddy-kimi      # Kimi-K3
ccp -m codebuddy-gpt       # GPT-5.5
ccp -m codebuddy-gemini    # Gemini-3.5-Flash
ccp -m codebuddy-hunyuan   # Hy4-Preview (free)
ccp models codebuddy       # everything the plan exposes (discovered live)
```

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
└───┬──────────────┬──────────┬───┘
    │              │          │
    ▼              ▼          ▼ OpenAI /chat/completions
┌────────────┐ ┌──────────┐ ┌────────────┐
│ CommandCode│ │ ClinePass│ │ CodeBuddy  │
│ (DeepSeek) │ │(Token Pl)│ │ (multi-LLM)│
└────────────┘ └──────────┘ └────────────┘
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
3. Register a `systemd --user` service for LiteLLM. The **default** install puts
   `ccp-sse-watchdog.service` on `127.0.0.1:4000` with LiteLLM behind it on `:4001`.
   `--no-watchdog` installs the single-process shape instead, which is **lossy and
   not equivalent** — read [Deployment modes](#deployment-modes) before using it.

### Deployment modes

Every provider profile connects to `127.0.0.1:4000`. Two shapes can sit behind that
port, and **they are not equivalent.** The watchdog is not just an SSE stream
cleaner — it is the only place the request-rewriting layer is implemented, so the
single-process shape silently drops it.

**With watchdog (default, complete)** — LiteLLM on `:4001`, `sse_watchdog` on `:4000` in front:

    claude ──> :4000 sse_watchdog ──> :4001 LiteLLM ──> upstream

```bash
bash bridge/setup_bridge.sh          # default
```

`sse_watchdog.py` owns four rewrites. They are implemented in `bridge/sse_finalize.py`
and called from **nowhere else** in the repository:

| Rewrite | Why it is needed |
| :--- | :--- |
| `strip_1m_model` | Removes the `[1m]` suffix carried by the registry model names, which strict upstream gateways reject |
| `strip_1m_beta` | Drops the `anthropic-beta` 1M-context token that otherwise triggers `400` on strict proxies |
| `clamp_max_tokens` | Caps `max_tokens` at the discovered output limit (393216), fixing `400 Invalid max_tokens value` |
| `sanitize_channel` | Satisfies a gateway's *client-identity check*. CodeBuddy/WorkBuddy reject a chat with `400 {"code":11128,"msg":"Illegal API invocation from an unapproved channel"}` when it carries Claude Code's `x-anthropic-billing-header` system block, **or** when the first system message *opens* with Claude Code's identity line. The billing block is dropped and a refused opening gets one leading newline. Driven by `bridge/request_rules.json`, so it applies **only** to providers listed under `request_rules:` in `registry/profiles.yaml` — Anthropic's own API still gets the header |

It also closes the Anthropic SSE stream after a complete `message_stop` and drops
trailing keepalives.

**⚠️ Single process (lossy — not equivalent)** — LiteLLM owns `:4000` directly:

    claude ──> :4000 LiteLLM ──> upstream

```bash
bash bridge/setup_bridge.sh --no-watchdog
```

One process, one hop, nothing to keep in sync — but LiteLLM performs **none** of the
four rewrites above. `config.yaml` declares `model_info` limits but has no callbacks,
hooks, `drop_params` or output-cap enforcement, so nothing rewrites the request. A real
Claude Code client on a 1M-context model sends all three problematic things at once
(`max_tokens: 1000000`, a model name ending in `[1m]`, and the 1M beta header) and
upstream answers `400`.

This is why local `curl` smoke tests look fine and the client still fails: a hand-written
request does not carry those three attributes. The single-process shape is only viable if
your upstream already tolerates raw 1M requests.

Re-running it also removes a `ccp-sse-watchdog.service` left behind by an earlier
watchdog install, which would otherwise fight LiteLLM for the port.

> **History worth knowing.** An earlier idle-injection bug in the watchdog
> spliced synthetic JSON into a live stream. LiteLLM rejected that as
> `MidStreamFallbackError` and Claude Code then retried for roughly ten minutes.
> That path is gone — idle injection is disabled by default and the injector was
> removed. Do **not** "fix" it by switching to single-process: that trades one
> already-fixed bug for three missing rewrites.

Check which one you are on:

```bash
systemctl --user status litellm-cmdc ccp-sse-watchdog
ss -ltnp | grep 4000
```

If `:4000` belongs to `litellm` you are on the lossy single-process shape; if it
belongs to `sse_watchdog.py`, LiteLLM is behind it on `:4001` and you are on the
complete path.

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

## 🐧 Using CodeBuddy with Claude Code

[CodeBuddy](https://www.codebuddy.ai/cli) (Tencent coding-copilot) is a flat
subscription that fronts a whole fleet of third-party models — DeepSeek, GLM,
Kimi, GPT, Gemini, Hunyuan. It has **no API-key product**: the gateway
authenticates the session the CodeBuddy Code CLI already holds, so `ccp`
imports that session.

### 1. Import the local CodeBuddy login

```bash
codebuddy          # log in once with the CodeBuddy CLI, if you have not
ccp auth codebuddy # import the token + verify it against the gateway
```

`ccp auth codebuddy` reads the plaintext session file the CLI writes

    ~/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info

and writes `CODEBUDDY_API_KEY=<access-token>` to
`~/.config/litellm/proxy.env` (which the systemd unit loads and the generated
config references as `os.environ/CODEBUDDY_API_KEY`). It then streams one tiny
completion to prove the token really works. Override the search with
`--auth-file PATH`, or point discovery at a specific file with
`CODEBUDDY_AUTH_FILE` / `CODEBUDDY_API_KEY`.

> The **desktop** CodeBuddy client (≥ 5.6.0) seals its token in an AES-GCM
> envelope only that binary can open. Import from the **CLI** login instead —
> it stores the session in the clear, and the command says so if it finds an
> encrypted file. `codebuddy_auth.py` reports *which* candidate it rejected and
> why (missing / not JSON / no `auth` / sealed), so a failure is never just
> "not logged in".

### How ccp handles credentials

Credential handling here follows the rules in magpie's
[`docs/subsystems/providers-accounts.md`](https://github.com/yetone/magpie/blob/main/docs/subsystems/providers-accounts.md),
restricted to the parts that apply to a tool which does **not** own the account:

| Rule | Why, and what it means here |
| :--- | :--- |
| **Read, never refresh** | CodeBuddy rotates the refresh token on every refresh, and the CLI is its single holder — refreshing here would rotate it out from under the CLI and sign it out. `ccp` only ever reads. When the access token lapses, `ccp` says so and tells you to run `codebuddy` once, which renews its own session in place. |
| **Adopt a rotated token automatically** | `ccp bridge restart` / `start` re-reads the source of truth, so a session the CLI renewed is picked up instead of silently going stale in `proxy.env`. Silent when CodeBuddy is not in use. `ccp auth codebuddy --status` reports in-sync / out-of-sync / expiry without changing anything. |
| **An unreadable store is an error, never an empty one** | `proxy.env` holds every provider's key. If it exists but cannot be read, the import fails loudly rather than rewriting it from an empty read and dropping the rest. |
| **Durable, atomic writes** | Temp file in the same directory, `flush` + `fsync`, then `os.replace`, mode `0600`. magpie learned this the hard way — a crash left its state files at full length with every byte zero. A zeroed `proxy.env` would take down every provider at once. |
| **Secrets are never resurrected from a backup** | No `.bak` is kept for `proxy.env` (magpie refuses to read its key store from one: "a key revoked in the last write would come back"). The config backups `ccp bridge deploy` makes are safe to keep, because the generated config references `os.environ/…` and holds no key. |
| **Say when a credential cannot renew itself** | A session with no `refreshToken` is reported rather than silently used — it cannot outlive its access token. |

Deliberately **not** adopted: magpie's account pools, quota tracking and
routing groups. Those exist because magpie owns and switches accounts across
many agents; `ccp` switches providers for one agent, and taking ownership would
mean becoming the CLI's sign-in holder — the thing the first rule forbids.

### 2. Restart the bridge & switch

```bash
ccp discover codebuddy   # pull the model catalogue your plan exposes
ccp render               # regenerate config + profiles + clamp/rule maps
ccp bridge restart       # deploy the rendered config + load CODEBUDDY_API_KEY
ccp codebuddy            # switch
```

`ccp bridge restart` (and `start`) first deploy `bridge/config.generated.yaml`
to `~/.config/litellm/config.yaml`, backing up the previous file — so a `ccp
render` is always followed by a service that actually serves it. Use
`ccp bridge deploy` to do just the copy. A hand-edited config that still holds
literal `api_key:` values is never overwritten; run `bash bridge/setup_bridge.sh`
once to migrate those into `proxy.env`.

### 3. Why CodeBuddy needs more than a base URL

Three things about this gateway are not obvious, and all three are handled
declaratively so nothing is hard-coded in the proxy:

| Constraint | How `ccp` handles it |
| :--- | :--- |
| Only `/v2/chat/completions` exists — `/v1/*` and the OpenAI **Responses API** 404 | The deployment uses LiteLLM's `hosted_vllm/` prefix instead of `openai/`. LiteLLM 1.103 sends every `openai/` deployment reached through `/v1/messages` to the Responses API, which would POST `/v2/responses` and 404. The global opt-out (`LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES`) would re-route every *other* provider too, so the prefix is fixed per deployment in `render_config.py` |
| The gateway has a **channel check**: a chat is rejected with `400 {"code":11128,"msg":"Illegal API invocation from an unapproved channel"}` if it carries Claude Code's `x-anthropic-billing-header` block, or if the first system message *opens* with Claude Code's identity line | The watchdog's `sanitize_channel` rule (CodeBuddy models only, via `request_rules` in `registry/profiles.yaml` → `bridge/request_rules.json`) drops the billing block and gives a refused opening one leading newline. Both are needed: removing the billing block *promotes* the identity line into the inspected position, and Claude Code 2.1.287 sends a refused identity in the interactive TUI and under `--append-system-prompt` (only plain `-p` sends the accepted `You are a Claude agent, built on Anthropic's Claude Agent SDK.`). Anthropic's own API still receives the original bytes |
| Streaming is mandatory (non-stream → `400 11101`) | Claude Code always streams, and the bridge is a streaming proxy, so nothing extra is needed — but a plain `curl` against the upstream without `"stream": true` will fail |

Model metadata (context window, output cap, vision) is discovered from
`GET /v2/enterprises/personal/models`, which is **account-scoped**: `ccp models
codebuddy` shows exactly what your plan exposes. `discover_models.py` re-adds a
few ids the gateway routes but the INTL catalogue omits (notably
`deepseek-v4.1-flash`, the free 1M multimodal one that the default tier uses).

The CN deployment (`copilot.tencent.com`) works the same way; change
`models_url`/`api_base` in `bridge/discover_models.py` if your account lives
there.

---

## 🔀 Accounts, pools and routing groups

These three exist so a plan running out is not your problem. All of it is
**native LiteLLM** — ccp only writes the config that turns it on, and adds no
router of its own.

### Several accounts for one provider

An account is another deployment of the *same* model name, differing only in
which credential it uses. LiteLLM load-balances across them, and when one
answers 401/429 it is cooled down and skipped — so an exhausted or expired
login stops being used without the client knowing it exists.

```bash
ccp accounts add codebuddy a2       # import the CLI's login under a new name
ccp accounts add cmdc work --token user_xxx
ccp accounts list                   # what is configured, and whether each key is set
ccp accounts remove codebuddy a2
ccp render && ccp bridge restart
```

Credentials go to `~/.config/litellm/proxy.env` (0600) as
`<PROVIDER>_API_KEY_<SUFFIX>`; only the *name* of that variable is recorded in
`registry/profiles.yaml`, which is committed. Removing an account leaves the
secret alone — ccp does not delete a credential you may still need.

> Verified against the live gateway: with a second CodeBuddy account holding an
> invalid token, 8/8 requests still succeeded, and the bad deployment was tried
> exactly once before being cooled down.

### Routing groups: one name, several models

A group is one model name with a deployment per `(member, account)` pair, so a
group is also a pool. Use it as the model and failover happens underneath.

```bash
ccp accounts group add daily codebuddy codebuddy-fast cmdc-deepseek-fast \
    --strategy usage-based-routing-v2 --fallback cmdc-deepseek-pro
ccp -m group/daily
```

| `--strategy` | Behaviour |
| :--- | :--- |
| `simple-shuffle` (default) | random, weighted |
| `usage-based-routing-v2` | send to whoever has used least of its limit |
| `least-busy` | fewest in-flight requests |
| `latency-based-routing` | fastest responding member |
| `cost-based-routing` | cheapest member |

`--fallback` names what to use once every member is exhausted. Members may be
aliases (`codebuddy`), registry ids (`glm-5.3`) or `provider/model`.

### Quota

```bash
ccp quota              # every provider that publishes a usage API
ccp quota codebuddy    # just one
```

```
codebuddy  (CODEBUDDY_API_KEY)
  Bonus Pack                   █████████████████░░░░░░░  75.9 left of 250 credit
                               resets 2026-10-24 08:59:35
  Free Plan Subscription       ░░░░░░░░░░░░░░░░░░░░░░░░  100.0 left of 100 credits
                               resets 2026-10-31 23:59:59
```

Only providers with a real usage endpoint are listed — CodeBuddy's
`/v2/billing/meter/get-user-resource` today. ccp does not invent numbers for
providers that publish none, and readings are cached for two minutes because
these endpoints are rate limited.

### What this deliberately does not do

magpie's pools also *switch the agent's own login* and track per-account quota
to pick which to use. ccp does neither: it never refreshes or rotates a
credential it does not own (see [How ccp handles credentials](#how-ccp-handles-credentials)),
and it lets LiteLLM's cooldown decide which account is out rather than
predicting it from quota. That keeps the "one holder per refresh token" rule
intact and needs no background job.

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
