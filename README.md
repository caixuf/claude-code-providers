# 🚀 ccp (Claude Code Provider Switcher)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform: Linux / macOS / WSL](https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20WSL-green.svg)]()
[![Tested on: Claude Code](https://img.shields.io/badge/Claude%20Code-Compatible-purple.svg)]()

> **Switch the model behind Claude Code in one command, across seven providers** —
> native Anthropic endpoints, OpenAI-compatible gateways, and a local adapter for
> a subscription that publishes no API at all.

Claude Code speaks one protocol (`/v1/messages`, Anthropic Messages) and expects
one shape of model metadata. `ccp` makes every provider look like that: a
`~/.claude/providers/<name>.json` profile pins Claude Code's tiers to real
models, a committed registry records what each model actually is, and a local
bridge translates where the vendor does not already speak Anthropic.

```
claude ──> ccp profile ──┬─> vendor's /anthropic endpoint         (native)
                         ├─> :4000 watchdog ─> :4001 LiteLLM ──> (bridged)
                         └─> :4100 local adapter ─────────────> (subscription)
```

---

## 📇 What you get

| Folder | Lands on (default) | How it is reached | Models |
| :--- | :--- | :--- | ---: |
| `deepseek/` | DeepSeek-V4-Flash (1M) | native Anthropic | 2 |
| `minimax/` | MiniMax-M3.1-Flash-Preview (1M) | native Anthropic | 9 |
| `opendesign/` | DeepSeek-V4.1-Flash (1.05M) | native, Anthropic-native aggregator | 11 |
| `qianfan/` | DeepSeek-V4.1-Flash (1.44M) | bridged → LiteLLM | 6 |
| `cline/` | DeepSeek-V4.1-Flash (1M) | bridged → LiteLLM | 6 |
| `codebuddy/` | DeepSeek-V4.1-Flash (1M) | bridged → LiteLLM | 15 |
| `trae/` | Qwen3.8-Max (1M) | local adapter `:4100` | 19 |

Several folders start on the *same* underlying model. That is the point: same
model, different pipe, price and quota. Every folder also holds the other models
its provider serves, so nothing is a dead end.

---

## 📦 Install

```bash
git clone https://github.com/caixuf/claude-code-providers.git ~/.claude-code-providers
cd ~/.claude-code-providers
bash install.sh              # add --bridge to set up the LiteLLM gateway too
```

`install.sh` **symlinks** `~/.local/bin/ccp` at `bin/ccp` rather than copying it.
A copy is how this goes wrong invisibly: `ccp` keeps running yesterday's build
while the checkout has moved on, so it lists a different set of providers than
the repo defines and new subcommands quietly do nothing. The symptom is a `ccp`
output that disagrees with this README.

```bash
ls -l ~/.local/bin/ccp      # must show '-> .../claude-code-providers/bin/ccp'
bash install.sh             # re-links it
```

Keys and tokens live in `~/.config/litellm/proxy.env` (mode `0600`), never in the
repo. Provider profiles are generated from `providers/*.json.example`.

---

## ⚡ Quick start

```bash
ccp                                  # list providers; the active one is starred
ccp qianfan                          # switch to a provider's default model
ccp trae deepseek-v4.1-flash         # switch provider *and* model
ccp trae/deepseek-v4.1-flash         # same thing, as a path
ccp -m codebuddy-glm                 # change model inside the current provider
ccp -c                               # show what is active, and where it points
ccp ping trae -s                     # benchmark this provider's models, switch to the winner
ccp --rollback                       # undo the last switch
```

Inside a running session, `/model` switches between Opus / Sonnet / Haiku — each
is pinned by the active profile to a real model.

---

## 🧭 Concepts

### A profile is a provider, not a model

`ccp` lists one folder per provider; a folder's contents are its models. Model
names match case-insensitively and are written back in the vendor's own spelling
(`ccp trae deepseek-v4.1-flash` → `DeepSeek-V4.1-Flash`), because normalising it
would 404 upstream.

How a folder's contents are spelled depends on how it is reached, and
`bridge/ccp_pick.py` is what keeps that honest: a **native** folder lists the
vendor's own ids, a **bridged** one lists the LiteLLM alias with the provider
prefix stripped. That is why `ccp trae kimi-k3` and `ccp qianfan glm-5.3` read
the way they do.

### One registry, discovered not typed

Every fact about a model — context window, output cap, whether it takes images —
lives in `registry/models.yaml` and is **auto-discovered**:

```bash
ccp discover [provider]   # pull the catalogue from each provider's own API
ccp render                # regenerate config + profiles from the registry
```

Discovery merges, in priority order:

1. **The provider's own catalogue** — Qianfan (`context_length`,
   `max_completions_tokens`, `architecture.input_modalities`), OpenDesign
   (`metadata.context_limit` / `context_budget`), DeepSeek, CodeBuddy
   (`maxInputTokens` / `supportsImages`), or, for Trae, the CLI the
   adapter already drives.
2. **LiteLLM's bundled model map** (`litellm.model_cost`, ~4500 entries) for
   anything the vendor does not publish.
3. A tiny defaults table for the handful of facts neither knows.

`registry/profiles.yaml` is the *only* hand-authored file: it records choices —
which model each Claude Code tier maps to, the aliases, which request rewrites
apply. `ccp render` joins the two into:

| Output | Purpose |
| :--- | :--- |
| `~/.config/litellm/config.yaml` | LiteLLM routing + per-model limits |
| `bridge/clamp_map.json` | model → output cap, read by the watchdog |
| `bridge/request_rules.json` | model → request rewrites, read by the watchdog |
| `providers/<name>.json.example` | profile with the right `CLAUDE_CODE_MAX_CONTEXT_TOKENS` |

Two discovery rules are load-bearing, because a catalogue is a *claim* and only a
call is evidence:

- **`only_models`** narrows a provider whose catalogue is wider than what the
  plan can call. ClinePass advertises 469 models and serves 6; Qianfan lists ~34
  and serves the GLM/DeepSeek families; CodeBuddy's CN plan lists ~33 of which
  half are legacy rungs. The registry keeps only the ids verified to answer, and
  every surviving id is aliased — so nothing the registry advertises is
  unreachable from its own folder.
- **`ctx_default`** overrides a vendor that under-reports its window; Trae's
  catalogue says 96k–184k and the real answer is 1M (the measurement is in the
  Trae section below).

---

## 🎛️ Commands

| Command | What it does |
| :--- | :--- |
| `ccp` | List providers; the active one is starred |
| `ccp <provider> [model]` | Switch provider, optionally to a specific model |
| `ccp -m <model>` | Hot-switch model inside the current provider |
| `ccp -c` / `ccp -r [N]` | Show active state / roll back N backups |
| `ccp models [provider]` | List models with context, output cap and vision |
| `ccp discover [provider]` | Refresh model metadata from the provider APIs |
| `ccp render` | Regenerate config + profiles + clamp/rule maps from the registry |
| `ccp sync` | Reconcile local profiles with the repo templates |
| `ccp ping [provider\|model]` | Probe health, TTFT and token speed |
| `ccp auth [copilot\|codebuddy\|trae]` | Authenticate / import a CLI login |
| `ccp bridge [status\|restart\|start\|stop\|deploy\|logs]` | Manage the local gateway and adapters |
| `ccp bridge {start\|stop\|restart}-trae` | Address the local Trae adapter |
| `ccp accounts …` | Extra logins and routing groups (see below) |
| `ccp quota [provider]` | What is left on each plan |
| `ccp -t` / `ccp --test` | Quick round-trip through Claude Code |

`ccp ping` rounds: default 2 rounds (~200 output tokens), `-q` 1 round (~20, for
liveness only), `-b` 3 rounds (~100, stricter). `--sort` picks the ranking key and
`-s` switches to the winner.

`ccp ping --agent` asks a different question: it sends one prompt with an obvious
action and times **the first tool call**, reporting `NEVER` when the model answers
in prose instead. For agent work TTFT is the wrong metric — a model can emit a
token instantly and still never act, which is the failure that wastes the turn.

---

## 🌉 The bridge

Three of the seven providers speak plain OpenAI `chat/completions` and are
**bridged**: Claude Code talks to a local gateway that translates on the way out.

```
claude ──> :4000 sse_watchdog ──> :4001 LiteLLM ──> upstream OpenAI gateway
```

```bash
bash bridge/setup_bridge.sh      # create the venv, patch, install units; --no-watchdog for the lossy shape
ccp bridge status                # what is running, on which port
```

`setup_bridge.sh` creates an isolated venv at `~/.local/share/litellm-venv`,
applies the upstream patches (ClinePass' `cline-pass/` prefix, the
`{"data":{"choices":…}}` envelope unwrap, dropping client headers strict proxies
reject), and registers the systemd units.

### Deployment modes — they are not equivalent

**With watchdog (default, complete).** `sse_watchdog.py` on `:4000`, LiteLLM
behind it on `:4001`. The watchdog is not merely an SSE cleaner: it is the only
place the request-rewriting layer exists.

| Rewrite (`bridge/sse_finalize.py`) | Why |
| :--- | :--- |
| `strip_1m_model` | Drops the `[1m]` suffix the registry names carry; strict gateways reject it |
| `strip_1m_beta` | Drops the `anthropic-beta` 1M-context token that triggers `400` upstream |
| `clamp_max_tokens` | Caps `max_tokens` at the model's discovered output limit, fixing `400 Invalid max_tokens value` |
| `sanitize_channel` | Satisfies a gateway's client-identity check (CodeBuddy, below). Applies only to providers listed under `request_rules:` in `registry/profiles.yaml`, because stripping Anthropic's billing header anywhere else is a silent prompt mutation |

It also closes the stream after a complete `message_stop` and drops trailing
keepalives.

**Single process (lossy).** `setup_bridge.sh --no-watchdog` puts LiteLLM on
`:4000` directly: one hop, nothing to keep in sync, and **none** of the four
rewrites. A real Claude Code client on a 1M model sends all three problematic
things at once (`max_tokens: 1000000`, a `[1m]` name, the 1M beta header) and
upstream answers `400`. This is why a `curl` smoke test looks fine and the client
still fails — the hand-written request does not carry those three attributes.

```bash
systemctl --user status litellm-cmdc ccp-sse-watchdog
ss -ltnp | grep 4000          # sse_watchdog.py = complete; litellm = lossy
```

> **History worth keeping.** An idle-injection bug once spliced synthetic JSON
> into a live stream; LiteLLM rejected it as `MidStreamFallbackError` and Claude
> Code retried for ~10 minutes. That path is gone (idle injection is disabled by
> default and the injector was removed). Do **not** "fix" anything by moving to
> single-process — that trades an already-fixed bug for three missing rewrites.

### Local adapter (Trae)

A service that publishes no API gets its own small Anthropic-speaking process,
`native: true` in the profile with the adapter (not the vendor) as the endpoint:

| Adapter | Port | Drives | Unit |
| :--- | ---: | :--- | :--- |
| `bridge/trae_bridge.py` | 4100 | TraeCLI's private `llm_raw_chat` RPC | `ccp-trae-bridge.service` |

Nothing in the running `:4000`/`:4001` deployment changes for it.

---

## 🔌 Providers

### Native Anthropic endpoints

`deepseek/` and `minimax/` point straight at the vendor's `/anthropic` endpoint —
no bridge, no rewrites. Their `/models` responses are rich
(`context_window`, `input_modalities`, and for MiniMax a plan-specific id list),
so discovery is exact.

MiniMax is worth a note: the catalogue is a function of the key. A standard key
gets 8 ids with no M3.1; the **M Plan** key gets 9 and `MiniMax-M3.1-Flash-Preview`
is among them. ccp reads the catalogue per key rather than pinning a list — but
does not trust it further than that. The endpoint does not reject a name it does
not serve (asking for `MiniMax-M3.1` returns 200 and a completion from `M3`), so
`ccp verify` reads the `model` field the response carries back.

### OpenDesign

[OpenDesign](https://amr-link.open-design.ai) is an Anthropic-native aggregator:
it speaks `/v1/messages` already, so it needs no bridge and no rewrites — the
profile points straight at it. It is a credit reseller, which is why its ladder
is built from the vendor's own `cost`/`capability` metadata rather than list
price. Its `/v1/models` is unusually complete
(`metadata.context_limit`, `metadata.context_budget`,
`architecture.input_modalities`), so discovery needs no cost-map cross-check.

> **Native profiles send the upstream id — never an alias or a `[1m]` marker.**
> Both exist only because the watchdog strips them on the bridged path; with no
> watchdog in the way a strict endpoint answers `model "opendesign[1m]" not
> found`. `render_config.py` therefore keeps aliases and the marker bridged-only.
> DeepSeek and MiniMax only appeared to work because their tiers happened to use
> ids with no alias — they were one rename away from the same failure.

### Qianfan (百度千帆 Token Plan)

A plain OpenAI-compatible gateway, so a normal bridged provider. Two properties
of the Token-Plan key are worth knowing, both verified live:

- **The key only works on the Token-Plan prefix.** The same key answers
  `token_plan_person_api_key_not_allowed` on `/v2/chat/completions`, so `api_base`
  is pinned to `https://qianfan.baidubce.com/v2/tokenplan/personal`.
- **The catalogue is far wider than the plan.** `/v2/models` lists ~34 models;
  Token Plan Person serves only the GLM and DeepSeek families.

`deepseek-v4.1-flash` takes the top tiers: 1.44M context, genuinely multimodal.

### ClinePass

469 models in the catalogue, **6 that actually answer.** Its `/v1/models` is a
public list, not an entitlement list: every Google, OpenAI, Anthropic and xAI id
returns `{"error":"model not found"}` when called. The request model must also be
`cline-pass/<id>` — without the prefix the gateway answers
`invalid model format`. `only_models` in `discover_models.py` keeps the registry
to the six verified ids, and the `cline-pass/` prefix is fixed per deployment in
`render_config.py` (dropping it silently bills pay-as-you-go instead of the flat
pass). Re-probe before extending that list.

### CodeBuddy (Tencent coding-copilot)

A flat subscription fronting DeepSeek, GLM, Kimi, MiniMax, MiMo, Hunyuan and
Step. It has **no API-key product**: the gateway authenticates the session the
CodeBuddy Code CLI already holds.

```bash
codebuddy          # log in once with the CLI
ccp auth codebuddy # import the token + verify it streams
ccp codebuddy      # switch; 15 models as `ccp codebuddy <name>`
```

`ccp auth codebuddy` reads the plaintext session the CLI writes
(`~/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info`)
and writes `CODEBUDDY_API_KEY` into `proxy.env`. The **desktop** client seals its
token in an envelope only that binary can open — import the **CLI** login; the
command says so when it finds an encrypted file.

**Two gateways, and a session works at exactly one.** CodeBuddy runs an
international site (`www.codebuddy.ai`) and a CN one (`copilot.tencent.com`,
behind `www.codebuddy.cn`). They do not share tokens, and a CN token sent to the
INTL edge answers a bare `401 invalid_token` that looks exactly like an expired
login. Which to use is derived from the login's own `auth.domain`
(`api_base()` in `bridge/codebuddy_auth.py`) and follows through to the bridge,
discovery and the quota reader; `CODEBUDDY_API_BASE` overrides it. The two fleets
differ — the CN plan carries no GPT or Gemini.

Bridged, not native: CodeBuddy speaks plain OpenAI. Three quirks are handled
declaratively:

| Constraint | How ccp handles it |
| :--- | :--- |
| Only `/v2/chat/completions` exists — `/v1/*` and the OpenAI Responses API 404 | The deployment uses LiteLLM's `hosted_vllm/` prefix instead of `openai/` (which LiteLLM 1.103 would route to `/v2/responses` → 404), fixed per deployment so no other provider is re-routed |
| A **channel check** rejects a chat with `400 {"code":11128,"msg":"Illegal API invocation from an unapproved channel"}` if it carries Claude Code's `x-anthropic-billing-header` block, or if the first system message *opens* with Claude Code's identity line | The `sanitize_channel` rule drops the billing block and gives a refused opening one leading newline. Both are needed: removing the billing block *promotes* the identity line into the inspected position. Anthropic's own API still gets the original bytes |
| Streaming is mandatory (non-stream → `400 11101`) | Claude Code always streams and the bridge is a streaming proxy, so nothing extra is needed |

### Trae CLI

TraeCLI (ByteDance's enterprise coding agent) publishes **no API at all**. A
logged-in TraeCLI holds a personal access token, exchanges it for a 14-day JWT,
and runs every turn through one private RPC:

```
POST https://api.enterprise.trae.cn/api/ide/v2/llm_raw_chat
```

`bridge/trae_bridge.py` replays that and re-shapes the SSE dialect into Anthropic
Messages on `:4100`.

```bash
ccp auth trae            # copy the TraeCLI token into proxy.env, then verify
ccp bridge start trae
ccp trae                 # ccp auto-starts the adapter if it is down
```

**Three protocol details that cost real time:**

1. **Tool `parameters` must be a JSON *string*.** An object makes Trae answer
   HTTP 500 with the bare body `Internal Server Error` and nothing else.
2. **Responses are an SSE dialect**, not OpenAI streaming: `event: output`
   carries a `tool_calls` array, `event: done` the `finish_reason`,
   `event: token_usage` the counts. An empty `response` with a populated
   `reasoning_content` is normal — thinking streams first.
3. **A turn that is only a tool call still carries `content: " "`** (one space).
   That is what makes the follow-up `tool_result` turn valid.

**Images work, but the tier has to be a vision model.** The adapter emits
OpenAI-style `image_url` data URLs correctly; the catch is that *TraeCLI* decides
whether pixels reach the model and substitutes
`[image/png content is not supported by current model]` on a text-only one. The
tiers are therefore all vision-capable (qwen3.8-max / Doubao-Seed-2.1-pro /
glm-5.3-flash); `glm-5.3`, the strongest tool-caller on the subscription, is
text-only and reachable through the `trae-glm-5.3` alias. Verified end to end:
Claude Code's `Read` on a PNG travels through the adapter and the model names the
colours correctly.

> **Trae's catalogue reports 96k–184k; the real window is 1M.** `prompt_max_tokens`
> is what *its own CLI* will put in one request, not the model's context. Taking
> it at face value cost 5/6 of the window. A needle at the very end of the
> context is retrieved at 333k, 555k and 888k input tokens; 1.25M returns HTTP
> 200 with empty content and zero usage — a silent failure, not an error.
> `ctx_default` overrides it, verified across four families. A request that
> returns 200 is not proof; only retrieving something from the far end is.

---

## 🔑 Credentials

Everything lives in `~/.config/litellm/proxy.env` (mode `0600`), which the
generated config references as `os.environ/…` and the systemd units load via
`EnvironmentFile`. The handling rules come from magpie's provider-accounts design
and are restricted to what applies to a tool that does **not** own the account:

| Rule | Why |
| :--- | :--- |
| **Read, never refresh** | CodeBuddy rotates the refresh token on every refresh and the CLI is its single holder; refreshing here would rotate it out from under the CLI and sign it out |
| **Adopt a rotated token automatically** | `ccp bridge start\|restart` re-reads the source of truth, so a session the CLI renewed is picked up instead of going stale in `proxy.env`. Silent when the provider is not in use |
| **An unreadable store is an error, never empty** | `proxy.env` holds every provider's key; rewriting it from a failed read would drop the rest |
| **Durable, atomic writes** | Temp file in the same directory, `flush` + `fsync`, then `os.replace`, mode `0600` — a crash must not leave a zeroed secrets file |
| **Secrets are never resurrected from a backup** | `proxy.env` keeps no `.bak`; a key revoked in the last write must not come back. Config backups are safe — they hold no key |
| **Say when a credential cannot renew itself** | A session with no `refreshToken` is reported, not silently used |

Deliberately **not** adopted: account pools that switch the agent's own login,
and quota-driven routing. ccp never takes ownership of a credential it does not
own, and lets LiteLLM's cooldown decide which account is out.

```bash
ccp auth copilot            # GitHub Copilot device flow
ccp auth codebuddy          # import the CodeBuddy CLI session
ccp auth trae               # import the TraeCLI personal access token
ccp auth codebuddy --status # in-sync / out-of-sync / expiry, changing nothing
```

---

## 🔀 Accounts, pools, routing groups and quota

All **native LiteLLM** — ccp only writes the config that turns it on.

An **account** is another deployment of the *same* model name, differing only in
which credential it uses. LiteLLM load-balances across them and cools one down
when it answers 401/429, so an exhausted login stops being used without the
client knowing it exists.

```bash
ccp accounts add <provider> a2 --token <key>   # a second credential, same model name
ccp accounts list
ccp accounts remove <provider> a2
ccp render && ccp bridge restart
```

Credentials go to `proxy.env` as `<PROVIDER>_API_KEY_<SUFFIX>`; only the *name*
of that variable is recorded in `registry/profiles.yaml`, which is committed.

A **routing group** is one model name with a deployment per `(member, account)`
pair, so a group is also a pool. Use it as the model and failover happens
underneath.

```bash
ccp accounts group add daily qianfan-deepseek-v4.1-flash \
    cline-deepseek-v4.1-flash --strategy usage-based-routing-v2 \
    --fallback opendesign-deepseek-v4-pro
ccp -m group/daily
```

| `--strategy` | Behaviour |
| :--- | :--- |
| `simple-shuffle` (default) | random, weighted |
| `usage-based-routing-v2` | whoever has used least of its limit |
| `least-busy` | fewest in-flight requests |
| `latency-based-routing` | fastest responding member |
| `cost-based-routing` | cheapest member |

Members may be aliases, registry ids, or `provider/model`.

```bash
ccp quota              # every provider that publishes a usage API
```

CodeBuddy is the only provider here with a billing reader; its host is resolved
from the login like every other call, and a deployment that does not expose the
endpoint (the CN gateway answers `403`) is reported rather than guessed at.
Readings are cached for two minutes because these endpoints are rate limited.

> No provider in the current set uses accounts or groups yet — the commands were
> built for a second CodeBuddy or CommandCode login. They work; the examples
> above are illustrative.

---

## 📊 Benchmarks

`ccp ping <provider>` on this machine, identical prompt and settings for every
row (2 rounds, ~200 output tokens per round):

| Provider | Model | TTFT | tok/s | Total | Context |
| :--- | :--- | ---: | ---: | ---: | ---: |
| **qianfan** | `deepseek-v4-pro` | **759 ms** | 67.3 | 3.7 s | 1.4M |
| **qianfan** | `deepseek-v4.1-flash` | **781 ms** | 204.0 | 1.8 s | 1.4M |
| **qianfan** | `deepseek-v4-flash` | **957 ms** | 218.2 | 1.9 s | 1.4M |
| cline | `deepseek-v4-pro` | 1.5 s | 84.6 | 3.9 s | 1M |
| cline | `deepseek-v4.1-flash` | 1.6 s | 183.1 | 2.7 s | 1M |
| opendesign | `deepseek-v4-flash` | 2.2 s | 121.0¹ | 10.2 s | 1M |
| opendesign | `deepseek-v4.1-flash` | 2.4 s | 100.4¹ | 11.0 s | 1M |
| trae | `DeepSeek-V4.1-Flash` | 4.1 s | 156.9 | 4.6 s | 1M |
| trae | `DeepSeek-V4-Flash-Official` | 5.3 s | 363.8 | 7.4 s | 1M |
| trae | `DeepSeek-V4-Pro-Official` | 8.0 s | 88.7 | 8.8 s | 1M |

¹ OpenDesign buffers: it stalls, then releases the whole answer at once, so its
decode window is not measurable from the client. Those rounds are scored against
the whole request and flagged `buffered`. The same model once read **591 tok/s**
under a formula that divided by a decode window that never existed — a figure its
own 11 s total cannot support.

Qianfan wins on first token by roughly 2× against everything else, which is the
number that matters for an agent: that latency is paid on every turn.

### How the speed number is computed

`ccp ping` follows the harness's own readout, short enough to copy exactly:

```js
tps = stats.decodeTokens / (stats.decodeMs / 1e3)
```

Both counters accumulate across steps, `decodeMs` runs from the first token to
the last, and tool execution time never enters the denominator. `ccp ping` sums
across rounds instead of averaging per-round rates — averaging lets one
unrepresentative round pull the figure as hard as a long one, which is how the
same provider read 501 tok/s on one run and 194 on the next, minutes apart.

**Three traps this section exists to prevent:**

1. **A short sample measures nothing.** At ~20 output tokens the fixed cost
   dominates, and a real gap of under 10× looked like 35×. `-q` is for liveness
   only; never rank on it.
2. **Buffering inflates the rate.** A server that stalls and then dumps gives a
   decode window of nearly zero. Buffered rounds use the whole request and are
   marked, so the figure errs low rather than high.
3. **A ceiling is not a target.** `max_tokens` does not make a model produce that
   many tokens; the probe's prompt length scales with `max_tokens` so it does not
   stop at 30 regardless of the budget.

---

## 📦 Archived providers

CommandCode (out of credits) and GitHub Copilot (no healthy deployments) are
archived, along with what it takes to bring one back:
[`docs/archived-providers.md`](docs/archived-providers.md). That file also keeps
the CodeBuddy findings as an operator's reference.

---

## 📄 License

MIT License. Feel free to use, modify, and distribute!
