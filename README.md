# 🚀 ccp (Claude Code Provider Switcher)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform: Linux / macOS / WSL](https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20WSL-green.svg)]()
[![Tested on: Claude Code](https://img.shields.io/badge/Claude%20Code-Compatible-purple.svg)]()

> **Seamlessly switch LLM providers & models in Anthropic Claude Code CLI (`claude`).**  
> Supports both native Anthropic-compatible endpoints and OpenAI-compatible providers (DeepSeek, Qianfan, ClinePass, MiniMax, OpenDesign, Trae CLI).

---

## ✨ Features

- ⚡ **One-command switching**: Switch models in 1 second (`ccp qianfan`, `ccp trae`, `ccp deepseek`).
- 🛡️ **Safe & non-destructive**: Automatically creates timestamped backups of `~/.claude/settings.json` upon every switch; supports instant rollback with `ccp --rollback`.
- 🌉 **Universal Protocol Bridge**: Claude Code hardcodes the Anthropic Messages API (`/v1/messages`). `ccp` includes an optional zero-latency local proxy bridge (powered by LiteLLM) that allows Claude Code to access **any OpenAI-compatible gateway** (Qianfan, ClinePass, self-hosted vLLM) with full streaming, tool use, and thinking tokens!
- 🎯 **Preserves Full Capabilities**:
  - ✅ Tool calling (File search, editing, bash execution, LSP)
  - ✅ Thinking tokens (Extended reasoning / CoT streaming)
  - ✅ Stream-json / verbose output
- 🧪 **Battle-Tested**: Validated by rigorous automated 6-axis code generation benchmarks on complex real-world codebases.
- 🐧 **Baidu Qianfan (Token Plan)**: a plain OpenAI-compatible gateway, so it needs no adapter — just `ccp qianfan`.
- 🐣 **Trae CLI subscription**: TraeCLI publishes no API at all, so `bridge/trae_bridge.py` replays its private streaming RPC and re-shapes it into the Anthropic Messages API — `ccp trae`.

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
ccp qianfan
ccp trae
ccp deepseek
ccp cline

# Check current active profile and endpoints
ccp --current

# List models with auto-discovered context / output cap / vision
ccp models            # curated view
ccp models cline      # every model a provider exposes

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
   - Qianfan → `context_length`, `max_completions_tokens`,
     `architecture.input_modalities` (the richest of the kept providers)
   - OpenDesign → `metadata.context_limit` / `context_budget`
   - DeepSeek → `context_window`, `input_modalities`
   - Cline → id/name only
   - Trae → no `/models` at all; `bridge/trae_bridge.py` calls the
     subscription's own catalogue RPC
2. **LiteLLM's bundled model registry** (`litellm.model_cost`, ~4500 entries) for
   `max_input_tokens` / `max_output_tokens` / `supports_vision`.
3. A tiny defaults table for the few facts neither source knows.

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
for a 1M model), but OpenAI-compatible gateways hard-cap output well below that.
The local bridge (`bridge/sse_watchdog.py`) now clamps
`max_tokens` down to the model's discovered output cap before forwarding — so a
1M-context model works end-to-end instead of erroring with
`400 Invalid max_tokens value`.

> This clamp lives **only in watchdog mode**. In the single-process shape nothing
> rewrites the request, so the `400` comes straight back. See
> [Deployment modes](#deployment-modes) before choosing a shape.

### Multimodal

Vision-capable models are declared automatically (`supports_vision`), and a
two kept providers back image pasting:

```bash
ccp qianfan         # deepseek-v4.1-flash — 1.44M context, natively multimodal
ccp trae            # every tier is vision-capable; see the Trae CLI section
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

Pre-configured templates in `providers/` — the set ccp currently keeps:

| Profile | Provider | Path | Models |
| :--- | :--- | :--- | ---: |
| `qianfan` | Baidu Qianfan (Token Plan) | bridged → LiteLLM | 6 |
| `trae` | Trae CLI subscription | native `127.0.0.1:4100` | 23 |
| `cline` | ClinePass | bridged → LiteLLM | 469 catalogued |
| `opendesign` | OpenDesign aggregator | native Anthropic | 11 |
| `minimax` | MiniMax official | native Anthropic | 8 |
| `deepseek` | DeepSeek official | native Anthropic | 2 |

**Bridged** profiles run Claude Code → `:4000` watchdog → `:4001` LiteLLM →
upstream. **Native** ones point straight at an Anthropic-compatible endpoint.
A third shape exists for Trae: `native`, but the endpoint is a local adapter
rather than the vendor, because Trae speaks a private protocol.

Each profile pins Claude Code's four tiers to real models, so `ccp -m <alias>`
hot-swaps within a provider and `ccp <profile>` switches provider. Context,
output cap and vision are discovered from each provider's own API into
`registry/models.yaml` (see the section above), never hand-filled.

Archived providers — CommandCode, CodeBuddy and GitHub Copilot — are documented
in [`docs/archived-providers.md`](docs/archived-providers.md) along with what it
takes to bring one back.

## 🌉 Universal Bridge (LiteLLM Local Gateway)

Claude Code strictly issues requests in Anthropic Messages format (`/v1/messages`). Many providers (e.g. Qianfan, ClinePass, self-hosted vLLM) only provide OpenAI `/chat/completions`.

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
│  Qianfan   │ │ ClinePass│ │ self-hosted│
│ (GLM / DS) │ │(Token Pl)│ │    vLLM    │
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
| `sanitize_channel` *(currently unused — its only provider, CodeBuddy, is archived: see [docs/archived-providers.md](docs/archived-providers.md))* | Satisfies a gateway's *client-identity check*. CodeBuddy/WorkBuddy reject a chat with `400 {"code":11128,"msg":"Illegal API invocation from an unapproved channel"}` when it carries Claude Code's `x-anthropic-billing-header` system block, **or** when the first system message *opens* with Claude Code's identity line. The billing block is dropped and a refused opening gets one leading newline. Driven by `bridge/request_rules.json`, so it applies **only** to providers listed under `request_rules:` in `registry/profiles.yaml` — Anthropic's own API still gets the header |

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

## 🎨 Using OpenDesign with Claude Code

[OpenDesign](https://amr-link.open-design.ai) is an **Anthropic-native** aggregator:
it already speaks `/v1/messages`, so it needs **no bridge and no rewrites** — the
profile points straight at it. It is a credit reseller (a small top-up buys a
much larger amount of list-price usage), which is why its ladder is built from
the vendor's own `cost` / `capability` metadata rather than list price.

```bash
# 1. Add the key to the shared secrets file
#    OPENDESIGN_API_KEY=sk-amr-…
$EDITOR ~/.config/litellm/proxy.env

# 2. Discover its catalogue, render, sync, switch
ccp discover opendesign
ccp render && ccp sync
ccp opendesign
```

The upstream id goes into the live profile — the template keeps
`YOUR_OPENDESIGN_API_KEY`, so the key never reaches the repository.

| Tier | Model | Context |
| :--- | :--- | ---: |
| default / sonnet | `deepseek-v4.1-flash` | 1048k |
| opus / fable | `deepseek-v4-pro` | 1048k |
| haiku | `glm-5.3-flash` | 1048k |

Hot-swap within the aggregator with `ccp -m opendesign-pro`, `opendesign-glm`,
`opendesign-luna`, `opendesign-mimo`. Its `/v1/models` is unusually complete —
`metadata.context_limit` (window), `metadata.context_budget` (output cap) and
`architecture.input_modalities` — so `ccp discover` needs no cost-map
cross-check for it.

> **Native profiles send the upstream id, never an alias or a `[1m]` marker.**
> Both exist only because the watchdog strips them on the bridged path; with no
> watchdog in the way a strict endpoint answers
> `model "opendesign[1m]" not found`. `render_config.py` therefore keeps aliases
> and the 1M marker bridged-only. DeepSeek and MiniMax only appeared to work
> because their tiers happened to use ids with no alias — those two profiles
> were one rename away from breaking the same way, and are fixed here.

---

## 🐧 Using Baidu Qianfan with Claude Code

[Qianfan](https://qianfan.baidubce.com) (百度千帆) is a plain OpenAI-compatible
gateway, so it needs no adapter: it is a normal **bridged** provider behind
LiteLLM, like Qianfan or Cline.

```bash
# 1. Put the Token Plan key in the bridge's secrets file
#    (QIANFAN_API_KEY=bce-v3/...)
$EDITOR ~/.config/litellm/proxy.env

# 2. Refresh the catalogue, render, deploy
ccp discover qianfan
ccp render && ccp bridge deploy && ccp bridge restart
ccp qianfan
```

Two things about the Token Plan key are worth knowing, both verified against
the live endpoint:

* **The key only works on the Token-Plan prefix.** The same key answers
  `token_plan_person_api_key_not_allowed` on the ordinary
  `/v2/chat/completions`, so ccp pins `api_base` to
  `https://qianfan.baidubce.com/v2/tokenplan/personal`.
* **The catalogue is much wider than the plan.** `/v2/models` lists ~34 models,
  but Token Plan Person only serves the GLM and DeepSeek families — the ERNIE,
  Qwen, VL, OCR and embedding ids all answer
  `token_plan_person_model_not_supported`. Discovery therefore narrows the
  registry to the six ids the key can actually call, so no profile advertises a
  model that would fail at request time.

Qianfan's `/v2/models` is unusually good about metadata — `context_length`,
`max_completions_tokens` and `architecture.input_modalities` are all published
per model, so nothing has to be guessed or cross-checked against LiteLLM's cost
map. `deepseek-v4.1-flash` takes the top tiers: 1.44M context and genuinely
multimodal, so image pasting works on it.

> Aliases are spelled out (`qianfan-deepseek-flash`, `qianfan-glm-5.3`, …)
> because the short first cut (`qianfan-ds`, `qianfan-pro`) did not say which
> model you were actually on.

---

## 🐣 Using Trae CLI with Claude Code

TraeCLI (ByteDance's enterprise coding agent) is the odd one out here: it
publishes **no API at all**. A logged-in TraeCLI holds a personal access token,
exchanges it for a 14-day JWT, and then runs every turn through one private
streaming RPC:

```
POST https://api.enterprise.trae.cn/api/ide/v2/llm_raw_chat
```

So `bridge/trae_bridge.py` replays that RPC and re-shapes it into the Anthropic
Messages API. It listens on **`127.0.0.1:4100`** and Claude Code talks to it
directly — `native: true`, no LiteLLM hop, nothing in the running :4000/:4001
deployment changes.

```bash
ccp auth trae      # copy the TraeCLI token into proxy.env, then verify it answers
ccp bridge start trae
ccp trae           # switch; ccp auto-starts the adapter if it is down
```

Then just use `claude`. Verified against the live subscription: text, streaming,
multi-turn tool calling, and 23 models (GLM-5.3 / DeepSeek-V4 / Kimi-K3 /
MiniMax-M3 / Doubao-Seed / Qwen3.8-Max / MiMo).

To run it at login, install the unit once:

```bash
cp bridge/ccp-trae-bridge.service.example ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now ccp-trae-bridge.service
```

`ccp models trae` lists the catalogue, which `ccp discover trae` refreshes from
the subscription itself.

**Three protocol details this cost real time to find** — recorded here so nobody
re-derives them:

1. **Tool `parameters` must be a JSON *string*.** Sending an object makes Trae
   answer HTTP 500 with the bare body `Internal Server Error` and nothing else.
2. **Responses are an SSE dialect**, not OpenAI streaming: `event: output`
   carries a `tool_calls` array, `event: done` carries `finish_reason`, and
   `event: token_usage` the counts. An empty `response` with a populated
   `reasoning_content` is normal — thinking streams first.
3. **A turn that is only a tool call still carries `content: " "`** (one space).
   That is what makes the follow-up `tool_result` turn valid.

**Images work, but the tier has to be a vision model.** Trae takes an image as
an OpenAI-style `image_url` data URL on a user turn, and the adapter emits
exactly that. The catch is that *TraeCLI* decides whether pixels reach the
model: on a text-only model it substitutes
`[image/png content is not supported by current model]`. The profile's tiers are
therefore all vision-capable (qwen3.8-max / Doubao-Seed-2.1-pro /
glm-5.3-flash). `glm-5.3`, the strongest tool-caller on this subscription, is
text-only and is reached through the `trae-glm` alias when you do not need
vision.

Verified end to end: Claude Code's `Read` on a PNG travels through the adapter
and the model names the colours correctly.

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
> ⚠️ No provider in the current set uses accounts, pools or groups yet — they
> were built for CodeBuddy and CommandCode, both now archived. The commands
> work; the examples below are illustrative.

```bash
ccp accounts add <provider> a2 --token <key>   # a second credential, same model name
ccp accounts list                   # what is configured, and whether each key is set
ccp accounts remove <provider> a2
ccp render && ccp bridge restart
```

Credentials go to `~/.config/litellm/proxy.env` (0600) as
`<PROVIDER>_API_KEY_<SUFFIX>`; only the *name* of that variable is recorded in
`registry/profiles.yaml`, which is committed. Removing an account leaves the
secret alone — ccp does not delete a credential you may still need.

> Verified against the live gateway while CodeBuddy was still configured: with a
> second account holding an invalid token, 8/8 requests still succeeded, and the
> bad deployment was tried exactly once before being cooled down.

### Routing groups: one name, several models

A group is one model name with a deployment per `(member, account)` pair, so a
group is also a pool. Use it as the model and failover happens underneath.

```bash
ccp accounts group add daily qianfan-deepseek-flash cline-deepseek \
    --strategy usage-based-routing-v2 --fallback opendesign-pro
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
aliases (`qianfan-deepseek-flash`), registry ids (`glm-5.3`) or `provider/model`.

### Quota

```bash
ccp quota              # every provider that publishes a usage API
# ccp quota <provider>
```

*(No provider in the kept set publishes a usage API, so this prints nothing
today. It was written around CodeBuddy's `/v2/billing/meter/get-user-resource`,
archived with the provider.)*

ccp does not invent numbers for providers that publish no usage endpoint, and readings are cached for two minutes because
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

> Table recorded before the provider prune, so it still lists the CommandCode
> entry. Kept as a baseline: `claude-deepseek`, `claude-cline-deepseek` and
> `claude-minimax-m3` are all still in the kept set.

| Provider | Model | Quality Score | Generation Speed (tok/s) | Highlights |
| :--- | :--- | :---: | :---: | :--- |
| **`claude-deepseek`** | `deepseek-v4-flash` | **12.0 / 12** | **213.6 tok/s** | Blazing-fast raw generation speed |
| **`claude-cmdc-deepseek`** | `deepseek-v4.1-flash` | **11.8 / 12** | **58.4 tok/s** | Best overall consistency in deep multi-turn sessions |
| **`claude-cline-deepseek`** | `deepseek-v4.1-flash` | **12.0 / 12** | **69.8 tok/s** | High speed, flat monthly quota |
| **`claude-minimax-m3`** | `MiniMax-M3` | **10.8 / 12** | **58.9 tok/s** | Lowest latency, concise and direct answers |

---

## 📄 License

MIT License. Feel free to use, modify, and distribute!
