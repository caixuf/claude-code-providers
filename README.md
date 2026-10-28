# 🚀 ccp (Claude Code Provider Switcher)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform: Linux / macOS / WSL](https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20WSL-green.svg)]()
[![Tested on: Claude Code](https://img.shields.io/badge/Claude%20Code-Compatible-purple.svg)]()

> **Switch the model behind Claude Code in one command, across six providers** —
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
| `deepseek/` | DeepSeek-Flash (1M) | native Anthropic | 1 |
| `minimax/` | MiniMax-M3.1-Flash (1M) | native Anthropic | 2 |
| `opendesign/` | DeepSeek-V4.1-Flash (1.05M) | native, Anthropic-native aggregator | 1 |
| `cline/` | DeepSeek-V4.1-Flash (1M) | bridged → LiteLLM | 1 |
| `qianfan/` | DeepSeek-V4.1-Flash (1.44M) | bridged → LiteLLM | 1 |
| `trae/` | DeepSeek-V4.1-Flash (1M) | local adapter `:4100` | 1 |
| `opencode/` | DeepSeek-V4.1-Flash (1M) | native, Anthropic-native gateway | 1 |

Several folders start on the *same* underlying model. That is the point: same
model, different pipe, price and quota.

**Every one of them lands on a DeepSeek Flash**, except `minimax/` — MiniMax's
own M3 is a flat subscription, so it costs nothing to keep a second family
around. `opencode/` is a flat subscription too ($10/month), so it is kept for
the same reason. That is the whole curation rule now: a model that bills by the token
is only worth reaching when it is the cheap one, and every provider's
catalogue is narrowed (`only_models`) to exactly the model it exposes. A
GPT, a Kimi or a GLM rung that used to be one `ccp -m` away is gone — see
[Why only DeepSeek](#why-only-deepseek).

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
ccp restore --dry-run                # see how to get back to the pre-ccp settings
```

Inside a running session, `/model` switches between Opus / Sonnet / Haiku — each
is pinned by the active profile to a real model.

---

## ↩ Returning to the pre-ccp state

`ccp restore` puts back the `settings.json` ccp found before its first switch and
stops + disables the three user services (`litellm-ccp`, `ccp-sse-watchdog`,
`ccp-trae-bridge`). It never deletes profiles, keys, or the checkout, so
`ccp <provider>` works again immediately.

- The exact source is `settings.json.ccp-original`, captured by `ccp <provider>`
  the first time it replaces a file ccp did not render.
- Older installs have no snapshot; restore falls back to the **oldest**
  timestamped backup that is not ccp-rendered. A rendered profile is recognised
  by the `ANTHROPIC_DEFAULT_FABLE_MODEL` tier key, which ccp writes into every
  profile it emits and which no pre-ccp file contains; an early ccp profile that
  predates that key is still recognised by its base URL pointing at one of ccp's
  own loopback bridges (`:4000`, `:4001`, `:4100`). A gateway the user configured
  themselves — any other URL — is correctly treated as original.
  `CLAUDE_CODE_AUTO_COMPACT_WINDOW` is **not** a marker: people set it by hand,
  and treating it as one classified every original as ccp-made, which left the
  snapshot never captured and made `restore` report "No pre-ccp settings found"
  while holding a directory full of backups.
- The oldest backup can predate every provider key on the machine. When the
  chosen source carries no `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY`, the plan
  says so, because such a file only works if Claude Code is signed in to
  claude.ai (OAuth). Choose a later backup with `--from`, or use `--strip`.
- If neither exists, it refuses rather than guessing. `--strip` is the explicit
  opt-in: it removes only ccp-injected keys (`ANTHROPIC_BASE_URL`, the tier map,
  context window), preserving permissions, hooks and unknown keys.
- `--dry-run` shows the plan, `--yes` skips the prompt, `--keep-services`
  restores settings but leaves the bridges running, and `--from FILE` picks a
  specific backup.

Before replacing anything it writes another `settings.json.bak.<timestamp>`, so
the restore itself is undoable with `ccp --rollback`.

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
| `ccp restore` / `ccp reset` | Return to the pre-ccp settings and stop the bridges |
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
| `ccp hermes [provider]` | Point the local Hermes agent at a provider's model |
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

Two providers, `cline/` and `qianfan/`, speak plain OpenAI
`chat/completions` and are **bridged**: Claude Code talks to a local gateway that
translates on the way out. Every other folder takes one of the other two paths —
an Anthropic-native endpoint or the Trae local adapter — both covered below.

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
| `downgrade_media` | Replaces an image with a text marker for a model the registry records as text-only, fixing `400 Invalid content type. image_url is only supported by certain models` |
| `sanitize_channel` *(no provider uses it today)* | Satisfies a gateway's *client-identity check*: it strips the `x-anthropic-billing-header` block and neutralises Claude Code's identity line wherever a gateway inspects it. Applies **only** to providers listed under `request_rules:` in `registry/profiles.yaml`, because stripping the billing header anywhere else is a silent prompt mutation. Built for WorkBuddy, which is retired — `docs/workbuddy-channel-check.md` keeps the measurement |
| `normalize_auth` (`bridge/bridge_auth.py`) | Forwards the credential LiteLLM accepts, whatever the client sent. A stale key used to be answered `400 No connected db.`, and no key at all `500` — see below |

It also closes the stream after a complete `message_stop` and drops trailing
keepalives.

**Single process (lossy).** `setup_bridge.sh --no-watchdog` puts LiteLLM on
`:4000` directly: one hop, nothing to keep in sync, and **none** of the
rewrites above. A real Claude Code client on a 1M model sends all three problematic
things at once (`max_tokens: 1000000`, a `[1m]` name, the 1M beta header) and
upstream answers `400`. This is why a `curl` smoke test looks fine and the client
still fails — the hand-written request does not carry those three attributes.

```bash
systemctl --user status litellm-ccp ccp-sse-watchdog
ss -ltnp | grep 4000          # sse_watchdog.py = complete; litellm = lossy
```

> **History worth keeping.** An idle-injection bug once spliced synthetic JSON
> into a live stream; LiteLLM rejected it as `MidStreamFallbackError` and Claude
> Code retried for ~10 minutes. That path is gone (idle injection is disabled by
> default and the injector was removed). Do **not** "fix" anything by moving to
> single-process — that trades an already-fixed bug for three missing rewrites.

### The credential is the bridge's, not the client's

LiteLLM accepts exactly one credential: the master key in the config it was
started with. A client that carries another one is not answered "invalid key" —
it is answered `400 {"error":{"message":"No connected db."}}`, because the
virtual-key lookup wants a database and there is none. A client that carries
**no** key is worse: LiteLLM's own error handler imports `prisma`, which is not
installed, and the request dies with a `500`. Both read like the bridge is
down, and both are the client's mistake.

So the watchdog forwards the key LiteLLM actually enforces, whatever arrived —
right, stale, mistyped or missing. The client decides *which model* to ask for;
it does not decide whether the call happens. The key is read from the installed
LiteLLM config (`bridge/bridge_auth.py`), because that document is what
LiteLLM enforces — a hand-edited master key is the one that works — and the
committed constant is only the fallback for a machine whose config cannot be
read.

The live config should still say what it means, so the tools keep it honest:

* `ccp sync` rewrites the credential in the **active** `~/.claude/settings.json`
  when that profile points at the bridge. A native profile's credential is the
  vendor's own and is never touched.
* `ccp doctor` reports a bridge-pointing config that carries a credential the
  bridge will replace.

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

## 🤖 The Hermes agent

The local [Hermes](https://hermes-agent.nousresearch.com) agent reaches every
provider through the same bridge, so switching provider changes exactly one
thing for it: the model name it asks for.

```bash
ccp hermes                 # what the bot is running right now
ccp hermes qianfan         # point it at Qianfan's DeepSeek-V4.1-Flash
ccp hermes trae            # ...or at Trae's DeepSeek-V4.1-Flash
ccp hermes trae deepseek-v4.1-flash
ccp hermes qianfan --dry-run # show the change, write nothing
```

**Both Hermes layouts work.** A multi-profile install keeps one config per
profile under `~/.hermes/profiles/<name>/config.yaml`; a single-config install
keeps one at `~/.hermes/config.yaml` and has no `profiles/` directory at all.
The command finds whichever is actually there — the second used to die with a
`FileNotFoundError` traceback that said nothing — and `ccp hermes` reports
which layout it found.

The name written is the one the proxy **actually emitted** — the curated alias
when the registry has one, otherwise the bare registry id — resolved by the same
`litellm_key_for()` that `ccp render` used to write
`~/.config/litellm/config.yaml`. `minimax` therefore gets `MiniMax-M3.1-Flash`
and not a `minimax-MiniMax-M3.1-Flash` nobody serves; a name guessed rather than
resolved comes back as a 400 that reads like the bridge is down.

Two things this deliberately does not do:

* **It edits the model lines, not the file.** The Hermes config is hand-written
  and carries comments that matter — compression thresholds, the reason
  `telegram.enabled` is spelled out, the MCP command. Re-emitting the YAML
  through a parser would drop every one of them. Only the model lines move —
  `model.default` and `providers.ccp.default_model`, plus `model.provider` when
  a bot is being migrated onto ccp — and a sibling provider (`imfl`, the direct
  MiniMax fallback) is left byte-identical. The one thing it *adds* is the
  `providers.ccp` entry, when the bot has never been on ccp: it is appended as a
  top-level block (`providers:` is a top-level key in Hermes's schema) with
  every comment in the file left where it was. A bot parked on a provider ccp
  retired — `commandcode` ran out of credits — is the case this exists for, and
  refusing it left hand-editing as the only way across.
* **It bounces the gateway only after a real rewrite.** Hermes caches its agent
  at startup, so a rewritten config is inert until
  `hermes-gateway-<profile>.service` is restarted — and a bot still answering on
  the old model is worse than one that was never switched. `ccp hermes`,
  `--dry-run` and `--json` write nothing and restart nothing.

**Which profile, when you do not say.** `--profile` defaults to
`$HERMES_PROFILE`; failing that, to the profile whose gateway unit is installed —
and when more than one is installed, to the one that actually serves a messaging
platform, since a gateway with every platform off is cron and the local API, not
a bot. Anything still ambiguous is an error naming the profiles that exist,
never a guess: a guess here rewrites a live bot's config and restarts it.

`~/.hermes/active_profile` is deliberately **not** consulted. That file is the
sticky default for a bare `hermes` command, and it can name a profile with no
gateway at all — on the workstation where this was written it said `ccp` while
the running bot was `caixuf`, so the command would have rewritten a profile that
passes every check it makes (same `model.provider`, same block) and then bounced
a `hermes-gateway-ccp.service` that does not exist, leaving the real bot
untouched and no error anywhere in the output.

**A bot on an older provider moves, it is not refused.** Profiles that predate
ccp — on `commandcode`, `openrouter`, the bridged providers — are the ones with
somewhere to go, so `ccp hermes <provider>` migrates them: it rewrites
`model.default`, `providers.ccp.default_model` **and** `model.provider`, because
that last key is what picks the entry point. Writing only the model name would
read like a switch and behave like a no-op, since the bot would keep talking to
its old provider. A migration with no `providers.ccp` block to move to is
refused, and the message carries the block to paste.

A `.bak.<timestamp>` copy of the pre-change file is written first — note that
Hermes snapshots the profile's config into `<profile>/backups/config/` too, so
ours is filed there beside its own `.good.<timestamp>` entries rather than left
in the profile root. `--config` points the command at a file outside
`~/.hermes/profiles/` when you are testing.

> **One Feishu app, one gateway.** If a second host runs a gateway for the same
> profile, both connect to the same app and the same home channel, and messages
> are served by whichever one the platform picks — including the host you did
> not mean. Give the leftover profile `platforms.feishu.enabled: false` before
> pointing it anywhere else; it is otherwise a live copy of the bot.

---

## 🔌 Providers

### Native Anthropic endpoints

`deepseek/` and `minimax/` point straight at the vendor's `/anthropic` endpoint —
no bridge, no rewrites.

**A native base URL has no trailing `/v1`.** Claude Code appends `/v1/messages`
to whatever `ANTHROPIC_BASE_URL` says, verbatim, so a base that already ends in
`/v1` asks for `/v1/v1/messages` and the vendor answers `404` with no body —
the kind of failure that looks like a bad model id and is not. LiteLLM's
anthropic provider appends the same suffix to the base it is handed, so the
one convention serves both. `ccp sync` now notices a base URL that moved and
rewrites the profile instead of leaving it on the old address. DeepSeek's `/models` is rich (`context_window`,
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

### Qianfan (百度千帆 Token Plan)

[Baidu Qianfan](https://qianfan.baidubce.com) sells a Token Plan: a flat
subscription whose key is only accepted on the plan's own prefix,
`https://qianfan.baidubce.com/v2/tokenplan/personal`. The same key against
plain `/v2/chat/completions` answers `token_plan_person_api_key_not_allowed`,
so `api_base` is pinned to that prefix and the provider is bridged like
ClinePass.

Two things make it its own case rather than another OpenAI gateway:

- **The catalogue is a claim, not an entitlement.** `/v2/models` lists 35 ids;
  the plan serves nine. The other 26 answer HTTP 403
  `token_plan_person_model_not_supported`. `only_models` in
  `discover_models.py` keeps the registry to the verified set.
- **The verified set belongs to the subscription, not to the provider.** The
  2026-10-24 key serves nine ids where the previous one served six
  (`deepseek-v4.1-flash-1017`, `deepseek-v4-pro-0813` and `glm-5.1` are new).
  `bridge/qianfan_probe.py` walks the whole catalogue against the key in
  `proxy.env` and prints the allow-list to paste in — run it whenever the plan
  changes, never assume the old list still holds.

Its catalogue is also the only one here that publishes the facts outright:
`context_length`, `max_completions_tokens` and `architecture.input_modalities`
come with every model, so `norm_qianfan` needs no cost-map cross-check. The
DeepSeek rungs carry 1.44M context; `glm-5.3-flash` is vision-capable and
takes the haiku tier.

```bash
ccp qianfan             # switch to the folder
ccp models qianfan      # the nine ids this subscription serves
python3 bridge/qianfan_probe.py --emit-only-models   # re-derive the allow-list
```

Two operational notes. The key lives in `proxy.env`, which the systemd unit
loads as `EnvironmentFile` — a new key reaches the bridge only after
`systemctl --user restart litellm-ccp.service`, not after a `ccp render`. And
the catalogue endpoint answers `429 Over rate limit` when probed in a burst, so
the probe retries instead of concluding that the plan is gone.

**A text-only rung refuses the whole turn when it meets a picture.** Qianfan
answers `400 invalid_argument "Invalid content type. image_url is only supported
by certain models"`, and the picture is already in the conversation's durable
history — so *every* later turn of that session carries it and 400s the same
way. The session is not slow, it is dead. `deepseek-v4-flash-0731` is the rung
that does this; `deepseek-v4.1-flash` and `glm-5.3-flash` read images fine.

The bridge therefore rewrites the request instead of forwarding it: for a model
the registry records as text-only, `downgrade_media` replaces every image (and
every other media block) with a text marker naming the model, so the turn
completes and the reader knows the pixels never arrived. Which models need it
comes from the registry's *discovered* `vision` flag — `ccp render` derives the
list, so a model that gains or loses image input changes it with nobody editing
a second place.

### Why only DeepSeek

Every provider here exposes exactly one model, and on five of the six that
model is a DeepSeek Flash. MiniMax keeps its two, because its M3 plan is a
flat subscription and costs nothing to keep in the list.

The rule is a money rule. A relay that bills by the token sells several
families at very different rates, and the expensive one is one `ccp -m` away
from the cheap one — so a provider that can serve `gpt-6-astra` *and*
`deepseek-v4.1-flash` on the same key is a provider with a loaded gun in the
picker. Measured on yjapi.shop on 2026-10-28: `deepseek-v4.1-flash` cost
**$0.204 for 1.33M tokens**, `gpt-6-astra` cost **$5.42 for 1.04M tokens** —
26x the rate for the same volume of text, plus a ~4000-token system prompt the
relay injects into every GPT request. The account carried $2.

So the narrowing happens in three places, and none of them is a list somebody
maintains by hand:

- **the discovery layer** — each provider's `only_models` in
  `bridge/discover_models.py` names the one id the registry keeps, so the
  catalogue the bridge serves holds nothing else;
- **the curation layer** — `registry/profiles.yaml` pins all five Claude Code
  tiers of a provider to that same model, so no tier can drift onto an
  expensive one;
- **the probe** — `ccp ping` costs a ceiling before it starts (see below).

Restoring a rung is one edit in the first of those three, followed by
`ccp discover && ccp render && ccp sync`.

### The probe has a spend guard

A probe is a real request against a real account, and on a per-token relay
the bill is real. `ccp ping` therefore costs the run before it starts:

```bash
ccp ping                          # primary tiers, ~20k tokens, inside the default budget
ccp ping --all                    # REFUSED: --all is a bill, say how much you mean
ccp ping --all --budget-tokens 200000
ccp ping --tokens 100000 --rounds 3   # REFUSED: the plan does not fit the budget
```

The plan is `models x rounds x tokens`, refused outright when it does not fit
the budget — half-running it is worse than refusing, because the half that
ran is already spent. Each round claims its share as it goes, so a run that
turns out more expensive than planned stops instead of finishing, and the
table ends with what it spent:

```
probe spend: ~1,536 of 150,000 tokens
```

The default budget is 150k tokens, which covers a full sweep of the primary
tiers with room to spare. It is a *ceiling on the probe*, not on your
sessions.

### OpenCode Go (opencode.ai)

[OpenCode Go](https://opencode.ai/go) is a **flat $10/month subscription**, not
a per-token gateway: DeepSeek V4.1 Flash alone gets 34,670 requests per 5 hours
and 69,330 per month, worth $80 of list-price usage. It is kept for the same
reason MiniMax is — a model that costs by the token is only worth reaching when
it is the cheap one, and this one cannot run a balance down.

```bash
ccp opencode             # switch to it
ccp models opencode      # the one id this group serves
curl -s https://opencode.ai/inference/go/v1/models -H "Authorization: Bearer $OPENCODE_API_KEY"
```

**It is `native: true`, and that is the whole trick.** The console serves one
endpoint per family — `chat/completions`, `responses`, `anthropic/v1/messages`,
`google/v1beta` — and every text model is reachable through all of them, the
console doing the translation. But the gateway *refuses a request without a
conversation id*: "Request is missing x-opencode-session and cannot be routed
efficiently". The id it wants is the header Claude Code already sends
(`x-claude-code-session-id`), and Go recognises it as the native client's own.

Going through LiteLLM would drop that header: LiteLLM forwards client headers
only globally, and turning that on would push `anthropic-beta` at every
gateway in the fleet — a strict one answers 400. Talking to the vendor
directly lets the client's own headers arrive untouched, which is also what the
console asks for ("your client should send typical coding agent traffic").

**The endpoints are not interchangeable.** The catalogue splits the models
between them — GLM, Kimi, DeepSeek and MiMo on `chat/completions`; MiniMax
and Qwen on `messages` — and a model on the wrong one is refused outright.
Muse Spark 1.3 Contributor is a third case: it is served by `/responses` only,
and it answers `500` on the chat path.

**One model on purpose.** DeepSeek V4.1 Flash, on every tier: TTFT 1.6s,
242 tok/s, and the highest monthly allowance in the plan ($80). The plan
carries more — MiMo-V2.6-Flash is cheaper and reads audio and video — but this
account is cleared to exercise the DeepSeek Flash and the free models only, so
nothing else goes in the registry. A catalogue entry is not a licence to probe
it.

**Two models are out of reach from here, and it is not the key.** Muse Spark
1.3 Contributor (the cheapest model in the plan at $0.10/$0.20 per million
tokens) and Beam Free answer `403 RegionError "This model is not available in
your country"` — Meta's geographic use policy does not cover this region, and
the console says as much when it lists them as "limited regions". `iceman-
contributor-free` needs the workspace's Privacy setting flipped to allow
endpoints that train on request data.

**The region is a property of the request, not of the account.** From a
Chinese exit the gateway answers `403 RegionError — This model is not available
in your country` for the models Meta restricts by geography (Muse Spark, Beam
Free); through a US exit the same key gets `200` on the same model. The models
that train on request data are a different question again and never a
geography one: `iceman-contributor-free` and `muse-spark-1.3-contributor` both
answer *"Allow paid endpoints that train on request data in your workspace's
Privacy settings"* until that switch is flipped in the console.

Measured 2026-10-28: DeepSeek V4.1 Flash TTFT 1.6s / 242 tok/s, tool calls
and image input both work, and the response carries its own accounting — `"opencode": {"billing_source": "go",
"allowance": 0.0000117}` — so every call says what it cost against the monthly
allowance.

**Do not hammer it.** The gateway sits behind Cloudflare, and a burst of
requests trips its bot protection: the next call answers `403 error code:
1010` (the owner's browser-signature ban) for a minute or two, and a request
that gets through the ban comes back `401 Invalid credential` instead of the
real error. One probe at a time is the working rate.

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
`[image/png content is not supported by current model]` on a text-only one.

**The tiers are not uniform, so this depends on the tier you are on.** `opus` and
`fable` are Doubao-Seed-2.1-pro and `haiku` is glm-5.3-flash, all three
vision-capable; `default` and `sonnet` are DeepSeek-V4.1-Flash, which is
text-only. Because `default` and `sonnet` are what this profile starts on, image
work needs a vision tier selected explicitly: switch to `/model opus`, or name
`trae-doubao-seed-2.1-pro` directly. `glm-5.3`, the strongest tool-caller on the
subscription, is text-only and reachable through the `trae-glm-5.3` alias. With a
vision tier selected, this was verified end to end: Claude Code's `Read` on a PNG
travels through the adapter and the model names the colours correctly.

> **Trae's catalogue reports 96k–184k; the real window is 1M.** `prompt_max_tokens`
> is what *its own CLI* will put in one request, not the model's context. Taking
> it at face value cost 5/6 of the window. A needle at the very end of the
> context is retrieved at 333k, 555k and 888k input tokens; 1.25M returns HTTP
> 200 with empty content and zero usage — a silent failure, not an error.
> `ctx_default` overrides it, verified across four families. A request that
> returns 200 is not proof; only retrieving something from the far end is.

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
> hard stop. All three resolve to `deepseek-v4.1-flash`; the names stay separate
> because the client picks a tier by name, and a `pro` that quietly answered with
> a different model would be answering a question nobody asked. `main` leads with
> `opendesign` and falls back to `cline`; `pro` and `flash` do the same. They are
> illustrative in the same sense — swap the members for whatever you actually run.
>
> **Qianfan is the one member whose price depends on the clock** — full rate by
> day, 20% after 21:00 — so it is deliberately absent from the committed
> defaults: a failover that fires at midday bills five times what the same turn
> costs after dark. `bridge/qianfan_window.py` rewrites the marked block in
> `registry/profiles.yaml` and re-renders; the `ccp-qianfan-on` / `ccp-qianfan-off`
> timers run it at 21:00 and 08:00. Both are `Persistent`, so a machine asleep at
> 08:00 catches up instead of billing the night rate all day. `setup_bridge.sh`
> installs the units.

---

## 🩺 `ccp doctor`

Three things drift silently on a machine, and each one fails late and
confusingly:

* a **profile** left installed for a provider that was retired — a valid file,
  a live bridge, and the request only fails when somebody selects it, with a
  400 that reads like the bridge is down;
* the **active** profile naming a model the bridge no longer serves;
* the **Hermes bot** left on a retired model, which fails only when somebody
  messages it;
* a **credential** that is not the bridge's key — the calls still work, because
  the bridge forwards its own, but the config no longer says what it means.

```bash
ccp doctor          # what is installed, and what it points at
ccp doctor --json   # the same, machine-readable
```

The served set is **derived**, not a list anyone maintains: the repository's
rendered templates name the models the native providers answer to (they never
reach LiteLLM, so the proxy's own model list does not have them), and the
running bridge's `/v1/models` names every bridged deployment. A profile is
dead when none of the models it names is in that union.

`ccp sync` acts on the same judgement and archives the dead profiles into
`~/.claude/providers_disabled/`. It used to archive a hand-written list, which
was wrong in both directions: it did not know about a provider retired
yesterday, and it listed a provider that had come back.

## 📈 Token usage

The bridge sees every Anthropic-shaped `usage` block that reaches ccp, whichever
path the provider takes — native, LiteLLM, or a local adapter. That is the only
vantage point with a cross-provider view: providers reached natively never touch
LiteLLM, so LiteLLM's own spend table can never answer "what have I used this
week".

```bash
ccp usage                 # last 7 days
ccp usage --today
ccp usage --days 30 --by-model
ccp usage --by-provider
ccp usage --all --json
```

The ledger is `~/.local/state/ccp/usage.jsonl`, one line per finished request,
mode `0600`. Appending rather than rewriting a running total is deliberate: a
crash mid-write loses at most the last line, and any window can be re-aggregated
later without the writer having anticipated it. A rewritten total is the shape
that goes to zero when the process dies during the write.

**Four numbers, not one.** Anthropic reports fresh input, cache reads and cache
writes separately because they are priced differently, and `ccp usage` keeps
them apart:

```
  fresh input        470
  cache read       54.0k  (cheap tier)
  cache write       1200
  input total      55.7k
  output            1230
  grand total      56.9k

  97.0% of input was served from cache
```

Note how far the last line moves: a total counting only `input_tokens` would
report this session as 470 tokens instead of 55.7k of input. The cache is the
difference between a long session looking nearly free and looking like what it
costs.

**No prices are applied.** Every plan here prices fresh input, cache reads and
cache writes differently, and a stale multiplier is worse than no number — so
the counts are shown and you multiply. `--json` gives the raw figures for
whatever you bill against.

`--no-usage` on `sse_watchdog.py` turns recording off; the proxy is otherwise
unchanged.

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

CommandCode (out of credits), GitHub Copilot (no healthy deployments), OpenCode
and Antigravity (`agy`) are archived, along with what it takes to bring one back:
[`docs/archived-providers.md`](docs/archived-providers.md).

Two more were removed on 2026-10-28, with their reasons recorded there:
**Codex** (a GPT-only catalogue — every rung on it is a paid-per-token model,
and it is the one family the DeepSeek-only rule exists to keep out of reach)
and **壹佳API (yjapi.shop)**, whose $2 balance was spent by a context probe
against a 26x-priced model and which has not been recharged. MiniMax stayed:
its M3 plan is a flat subscription, so it costs nothing to keep.

---

## 📄 License

MIT License. Feel free to use, modify, and distribute!
