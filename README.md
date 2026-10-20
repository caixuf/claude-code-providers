# 🚀 ccp (Claude Code Provider Switcher)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform: Linux / macOS / WSL](https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20WSL-green.svg)]()
[![Tested on: Claude Code](https://img.shields.io/badge/Claude%20Code-Compatible-purple.svg)]()

> **Switch the model behind Claude Code in one command, across five providers** —
> native Anthropic endpoints, one OpenAI-compatible gateway, and a local adapter
> for a subscription that publishes no API at all.

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
| `minimax/` | MiniMax-M3.1-Flash (1M) | native Anthropic | 2 |
| `opendesign/` | DeepSeek-V4.1-Flash (1.05M) | native, Anthropic-native aggregator | 11 |
| `cline/` | DeepSeek-V4.1-Flash (1M) | bridged → LiteLLM | 6 |
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
ccp cline                            # switch to a provider's default model
ccp -m cline-glm-5.3                 # change model inside the current provider
ccp minimax MiniMax-M3.1-Flash       # native Anthropic: the vendor's own id, no bridge
ccp trae deepseek-v4.1-flash         # switch provider *and* model
ccp trae/deepseek-v4.1-flash         # same thing, as a path
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
prefix stripped. That is why `ccp trae kimi-k3` and `ccp cline glm-5.3` read
the way they do.

### One registry, discovered not typed

Every fact about a model — context window, output cap, whether it takes images —
lives in `registry/models.yaml` and is **auto-discovered**:

```bash
ccp discover [provider]   # pull the catalogue from each provider's own API
ccp render                # regenerate config + profiles from the registry
```

Discovery merges, in priority order:

1. **The provider's own catalogue** — OpenDesign (`metadata.context_limit` /
   `context_budget` / `architecture.input_modalities`), DeepSeek
   (`context_window`, `input_modalities`), Cline and MiniMax (ids only, so the
   rules below carry what the catalogue omits), or, for Trae, the CLI the adapter
   already drives.
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

Three discovery rules are load-bearing, because a catalogue is a *claim* and only a
call is evidence:

- **`only_models`** narrows a provider whose catalogue is wider than the set ccp
  curates. ClinePass advertises 469 models and serves 6 — every Google, OpenAI,
  Anthropic and xAI id answers `model not found` when called. MiniMax lists 9 ids
  and the M2.x ones still answer, but only 2 are on the vendor's documented
  supported set for the Anthropic-compatible interface, so the allow-list keeps
  those two. Every surviving id stays reachable from its own folder — as an alias
  on the bridged path, as its own upstream id on a native one.
- **`hints`** records a capability fact the catalogue does not carry, for a vendor
  whose `/v1/models` returns ids only. MiniMax is pinned that way: the
  `only_models` allow-list above for the two current ids, plus per-model `hints`
  for context, output cap and vision. `ccp discover` therefore neither
  re-introduces a retired id nor loses those numbers.
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
| `ccp auth [copilot\|trae]` | Authenticate / import a CLI login |
| `ccp bridge [status\|restart\|start\|stop\|deploy\|logs]` | Manage the local gateway and adapters |
| `ccp bridge {start\|stop\|restart}-trae` | Address the local Trae adapter |
| `ccp accounts …` | Extra logins and routing groups (see below) |
| `ccp quota [provider]` | What is left on each plan |
| `ccp -t` / `ccp --test` | Quick round-trip through Claude Code |

`ccp ping` rounds: default 2 rounds (~200 output tokens), `-q` 1 round (~64, for
liveness only), `-b` 3 rounds (~1000, stricter). `--sort` picks the ranking key and
`-s` switches to the winner. The per-round timeout follows the token budget, so a
slow model is not reported as a dead one.

`ccp ping --agent` asks a different question: it sends one prompt with an obvious
action and times **the first tool call**, reporting `NEVER` when the model answers
in prose instead. For agent work TTFT is the wrong metric — a model can emit a
token instantly and still never act, which is the failure that wastes the turn.

---

## 🌉 The bridge

One provider, `cline/`, speaks plain OpenAI `chat/completions` and is
**bridged**: Claude Code talks to a local gateway that translates on the way out.
Every other folder takes one of the other two paths — an Anthropic-native endpoint
or the Trae local adapter — both covered below.

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
| `sanitize_channel` *(currently unused — its only provider, CodeBuddy, is archived again: see [docs/archived-providers.md](docs/archived-providers.md))* | Satisfies a gateway's *client-identity check*: it strips the `x-anthropic-billing-header` block and neutralises Claude Code's identity line wherever a gateway inspects it. Applies **only** to providers listed under `request_rules:` in `registry/profiles.yaml`, because stripping the billing header anywhere else is a silent prompt mutation |

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

### The loopback never goes through the HTTP proxy

`:4000`, `:4001` and `:4100` are on this machine. A request to `127.0.0.1`
that the HTTP proxy answers for leaves the box, the proxy cannot route a
loopback destination, and it replies **`502` with an empty body** — which
reads exactly like a dead provider while the process behind it is healthy.

The trap is the spelling of `no_proxy`. Windows hands WSL a wildcard form
(`127.*`) and *no* HTTP stack understands it: curl, urllib and httpx read
exact hosts, domain suffixes and CIDR blocks, never `*`. So the loopback gets
proxied anyway.

`bridge/loopback.py` therefore enforces the bypass in the tool, next to the
sockets — the env is normalised (`127.0.0.1`, `::1`, `localhost` as exact
entries) and httpx pins the loopback to a direct transport. A tool cannot
depend on the shell it was launched from: non-interactive shells skip
`~/.bashrc`, a systemd service never reads it, and a shell that was already
open when the fix landed keeps the broken value.

`ccp` warns when the *shell* still has the broken spelling, because
everything else in the shell — Claude Code itself, `curl`, `python` — reads
`no_proxy` and would send its own loopback calls to the proxy:

```
⚠ no_proxy has no exact 127.0.0.1 entry ('127.*').
```

Fix it in the shell with:

```bash
export no_proxy="127.0.0.1,::1,localhost,$no_proxy"
```

---

## 🔌 Providers

### Native Anthropic endpoints

`deepseek/` and `minimax/` point straight at the vendor's `/anthropic` endpoint —
no bridge, no rewrites. DeepSeek's `/models` is rich (`context_window`,
`input_modalities`); MiniMax's is not, and that asymmetry is handled by discovery
(see the `hints` rule above).

MiniMax is worth a note. Its [Anthropic-compatible interface](https://platform.minimax.io/docs/api-reference/text-anthropic-api)
officially supports exactly two models — `MiniMax-M3.1-Flash` and `MiniMax-M3` —
and the catalogue still lists the M2.x family, which is legacy here and reachable
through the standard MiniMax API instead. Both current models are 1M-context
multimodal (text / image / video input), with a 524288 output cap (probed:
`max_tokens=524288` → 200, `524289` → 400 `does not support max tokens > 524288`).
`M3.1-Flash` takes the default and fable tiers and always thinks —
`thinking: {"type":"disabled"}` answers 400 — exposing `output_config.effort` from
`low` to `max` (default `max`); `M3` takes opus/sonnet/haiku, thinks off by
default, and enables it with `adaptive`.

`ccp` keeps a curated set rather than the whole catalogue, so the registry is
pinned to those two, and the M2.x rungs are gone from it. A catalogue is a claim,
not evidence: the endpoint does not reject a name it does not serve — asking for
`MiniMax-M3.1` returns 200 with a completion from `M3` in the body — so
`ccp <provider> <model>` verifies the pick by reading the `model` field the
response carries back (`bridge/ccp_pick.py verify`) and warns instead of letting
the session quietly run a different model. `SKIP_VERIFY=1` silences it.

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

### ClinePass

469 models in the catalogue, **6 that actually answer.** Its `/v1/models` is a
public list, not an entitlement list: every Google, OpenAI, Anthropic and xAI id
returns `{"error":"model not found"}` when called. The request model must also be
`cline-pass/<id>` — without the prefix the gateway answers
`invalid model format`. `only_models` in `discover_models.py` keeps the registry
to the six verified ids, and the `cline-pass/` prefix is fixed per deployment in
`render_config.py` (dropping it silently bills pay-as-you-go instead of the flat
pass). Re-probe before extending that list.

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
| **Read, never refresh** | A tool that owns the rotating refresh token must stay its only refresher — a second one rotates the token out from under the first and signs it out. That was CodeBuddy's shape (archived); the rule outlives it |
| **An unreadable store is an error, never empty** | `proxy.env` holds every provider's key; rewriting it from a failed read would drop the rest |
| **Durable, atomic writes** | Temp file in the same directory, `flush` + `fsync`, then `os.replace`, mode `0600` — a crash must not leave a zeroed secrets file |
| **Secrets are never resurrected from a backup** | `proxy.env` keeps no `.bak`; a key revoked in the last write must not come back. Config backups are safe — they hold no key |
| **Say when a credential cannot renew itself** | A session with no `refreshToken` is reported, not silently used |

Deliberately **not** adopted: account pools that switch the agent's own login,
and quota-driven routing. ccp never takes ownership of a credential it does not
own, and lets LiteLLM's cooldown decide which account is out.

```bash
ccp auth copilot            # GitHub Copilot device flow
ccp auth trae               # import the TraeCLI personal access token
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
ccp accounts group add daily cline-deepseek-v4.1-flash \
    opendesign-deepseek-v4.1-flash --strategy usage-based-routing-v2 \
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

*(No provider in the kept set publishes a usage API, so this prints nothing
today. CodeBuddy's reader went with it — see docs/archived-providers.md.)*
Readings are cached for two minutes because these endpoints are rate limited.

> No provider in the current set uses accounts yet — that part was built for a
> second CodeBuddy or CommandCode login, both archived. The three groups in
> `registry/profiles.yaml` (`main`/`pro`/`flash`) are curation, not usage: each
> pairs one subscription with another vendor's, so no single subscription is a
> hard stop. `main` and `pro` lead with `cline` and fall back to `opendesign`;
> `flash` leads with `opendesign-glm-5.3-flash` and falls back to `cline-glm-5.3`,
> because ClinePass has no flash-tier variant. They are illustrative in the same
> sense — swap the members for whatever you actually run.

---

## 📊 Benchmarks

`ccp ping <provider>` on this machine, identical prompt and settings for every
row (2 rounds, ~200 output tokens per round):

| Provider | Model | TTFT | tok/s | Total | Context |
| :--- | :--- | ---: | ---: | ---: | :---: |
| **opendesign** | `deepseek-v4-flash` | 1.4 s | **309.6** | **2.0 s** | 1M |
| opendesign | `deepseek-v4.1-flash` | 1.7 s | 279.3 | 2.4 s | 1M |
| cline | `deepseek-v4-pro` | **1.3 s** | 107.2 | 3.2 s | 1M |
| cline | `deepseek-v4.1-flash` | 2.0 s | 165.7 | 3.2 s | 1M |
| minimax | `MiniMax-M3` | 2.4 s | 92.4 | 4.4 s | 1M |
| minimax | `MiniMax-M3.1-Flash` | 4.7 s | 42.5² | 5.7 s | 1M |
| trae | `DeepSeek-V4.1-Flash` | 3.4 s | 63.8² | 3.5 s | 1M |
| trae | `DeepSeek-V4-Flash-Official` | 3.9 s | 166.5 | 5.0 s | 1M |
| trae | `DeepSeek-V4-Pro-Official` | 6.4 s | 45.5² | 6.6 s | 1M |

² **No decode window to measure**: the model thinks for seconds and then
releases the whole answer at once, so the speed is scored against the whole
request. Those figures are not comparable with the streaming rows, and the
probe says so under the table. The same model once read **591 tok/s** under a
formula that divided by a decode window that never existed — a figure its own
11 s total cannot support.

**This is one run, and the reasoning models swing.** `MiniMax-M3.1-Flash`
read 838 ms / 148.7 tok/s in a run minutes after the 4.7 s / 42.5 tok/s
above: how long a model thinks before it answers is erratic — the same
prompt produced 24, 422 or 494 reasoning tokens in three attempts. Rank on
the total, and read the `Think` column as part of the answer: it is the
wait paid on every turn, before the first useful token exists.

OpenDesign finishes a turn fastest here — `deepseek-v4-flash` in 2.0 s at
309.6 tok/s, and `deepseek-v4.1-flash` half a second behind it. Cline has
the first token (1.3 s) and pays for it in generation time. Trae is the
slowest to start — 3.4–6.4 s before the first token — and fast only once it
is decoding: `DeepSeek-V4-Flash-Official` moves 166.5 tok/s. Total time is
the column an agent feels, which is why the 3.2 s cline row beats trae's
3.5 s one despite half the throughput.

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
   decode window of nearly zero. Such rounds use the whole request instead, and
   the probe names them under the table, so the figure errs low rather than
   high.
3. **A ceiling is not a target.** `max_tokens` does not make a model produce that
   many tokens; the probe's prompt length scales with `max_tokens` so it does not
   stop at 30 regardless of the budget.

**The answer has to fit inside the budget.** The probe asks for a numbered list
sized at 40% of `max_tokens`, not 80%. Each number costs two or three tokens
once the separators are counted, and a model that reasons before it answers
spends part of the same budget on the reasoning. At 160 items and 200 tokens
the answer itself did not fit: the model was cut off mid-thought and produced
nothing — and the probe, which was counting the reasoning deltas as an answer,
reported a healthy 183 tok/s for a round in which the model had not said a
word. ClinePass returned `500 empty response content` about 65% of the time on
that shape. Half the budget for the answer leaves room to reason; the answer
is still long enough to time.

**A round that produced no answer is a failed round.** The stream was well
formed and the upstream was happy, so nothing in the transport said "error" —
but the user gets nothing, and a speed measured on a round without an answer
describes the reasoning, not the answer. Such a round is now reported as
`No Answer`, and the summary names it:

```
  ⚠ rounds that failed: group/pro (1/2 — no answer within the budget)
```

The quick check needed the same treatment: 20 tokens is less than the
reasoning alone costs on this fleet (19–45 tokens), so `-q` could never
succeed against a reasoning model and reported it as down. It now asks for 64.

**The deadline follows the budget.** The per-round timeout scales with
`--tokens` (five seconds of headroom per token, 15 s floor, 240 s ceiling) instead
of a flat 10 s. Trae's first byte alone ran past 17 s on the 200-token probe,
and 100 s at 1000 tokens; a flat 10 s cut it off mid-answer — so the row read
as a failure when the model was merely slow, and "trae is down" is a
conclusion a user acts on. `--timeout` still overrides it.

**A failed row says what failed.** The status keeps the whole code (`HTTP 502`,
not `HTTP 50` — the old seven-character cut turned a gateway error into a code
that does not exist), and an empty body is named rather than left blank: a
gateway error on a loopback endpoint is the proxy answering for it.

---

## 📦 Archived providers

CommandCode (out of credits), GitHub Copilot (no healthy deployments), CodeBuddy,
OpenCode, Antigravity (`agy`) and Baidu Qianfan (plan no longer used) are
archived, along with what it takes to bring one back:
[`docs/archived-providers.md`](docs/archived-providers.md).

---

## 📄 License

MIT License. Feel free to use, modify, and distribute!
