# 🚀 ccp — Claude Code Provider & Model Switcher

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform: Linux / macOS / WSL](https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20WSL-green.svg)]()
[![Tested on: Claude Code](https://img.shields.io/badge/Claude%20Code-Compatible-purple.svg)]()

`ccp` points Claude Code at whichever model and account you want, without
editing `~/.claude/settings.json` by hand. It keeps a committed registry of
models and providers, emits Claude Code profiles with the right tiers and
context window, and runs a local bridge for vendors that do not speak
Anthropic Messages directly.

```text
claude ──> ccp profile ──┬─> vendor /anthropic endpoint           (native)
                         ├─> :4000 watchdog ─> :4001 LiteLLM ──> (bridged)
                         ├─> :4100 Trae adapter ───────────────> (subscription)
                         └─> :4010 omp gateway ─> :8765 omp broker (accounts)
```

**ccp is a tool, not a product.** It holds no business knowledge and keeps no
conversation state. The rules that keep it that way are in
[PRINCIPLES.md](PRINCIPLES.md).

---

## What you get

| Folder | Default | How it is reached | Notes |
| :--- | :--- | :--- | :--- |
| `deepseek/` | `deepseek-flash` | native Anthropic | 1M context |
| `minimax/` | `MiniMax-M3.1-Flash` | native Anthropic | M3 as the fast tiers |
| `opencode/` | `deepseek-v4.1-flash` | bridged → LiteLLM | OpenCode Go subscription |
| `trae/` | `DeepSeek-V4.1-Flash` | local adapter `:4100` | text-only default; GLM/豆包 extras |
| `qpcmqam/` | `gpt-6.1-terra` | bridged → LiteLLM | personal relay |
| `groups/` | `main` | bridged → LiteLLM | `main` / `pro` / `flash` routing groups |
| `omp-opencode/` | `opencode-go/deepseek-v4.1-flash` | omp gateway `:4010` | dynamic, after `ccp omp` |

Several folders start on the same underlying model. That is intentional: same
model, different pipe, price, quota and latency. The picker is organised by
**provider**, and each provider exposes only the models this account was
curated for.

---

## Install

```bash
git clone https://github.com/caixuf/claude-code-providers.git ~/.claude-code-providers
cd ~/.claude-code-providers
bash install.sh                 # symlink ccp, install provider templates
bash bridge/setup_bridge.sh     # optional: LiteLLM + watchdog + Trae adapter
```

`install.sh` symlinks `~/.local/bin/ccp` at `bin/ccp`; a copy would silently go
stale while the checkout moves on. `install.sh --bridge` does both steps.

The bridge installs a Python venv at `~/.local/share/litellm-venv`, generates
`~/.config/litellm/config.yaml`, and registers user services:

- `litellm-ccp.service` — LiteLLM on `127.0.0.1:4001`
- `ccp-sse-watchdog.service` — SSE/request-rewrite proxy on `127.0.0.1:4000`
- `ccp-trae-bridge.service` — Trae adapter on `127.0.0.1:4100` (optional)

`ccp bridge status` is the one-command health check.

Optional multi-account login through **omp**:

```bash
# install omp separately first; ccp only drives its broker/gateway
ccp omp setup
ccp omp login
```

---

## Quick start

```bash
ccp                                  # list providers; * marks the active one
ccp opencode                         # switch to the OpenCode Go subscription
ccp -m opencode-deepseek-v4.1-flash  # switch model within the current provider
ccp groups                           # stay on main/pro/flash routing groups
ccp groups pro                       # start on the deliberate `pro` group
ccp omp opencode                     # omp-backed OpenCode, beside `ccp opencode`
ccp -c                               # what is active, and where it points
ccp ping                             # health + TTFT + token speed
ccp models opencode                  # context / output cap / vision
ccp doctor                           # is the install still consistent?
ccp restore --dry-run                # how to get back to the pre-ccp state
```

`ccp <provider> [model]` and `ccp <provider>/<model>` are equivalent:
`ccp trae DeepSeek-V4.1-Flash` == `ccp trae/DeepSeek-V4.1-Flash`.

---

## Concepts

### A profile is a provider, not a model

`ccp` lists one folder per provider; the folder's contents are its models.
Model names match case-insensitively and are written back in the vendor's own
spelling, because normalising an upstream id would 404.

A **native** folder lists the vendor's own ids; a **bridged** folder lists the
LiteLLM alias with the provider prefix stripped. `bridge/ccp_pick.py` is what
keeps that distinction honest.

### Tiers and `[1m]`

Every profile maps Claude Code's five slots — `default`, `opus`, `fable`,
`sonnet`, `haiku` — to real models. Inside a running session, `/model` switches
between those tiers without restarting Claude Code.

A bridged profile appends `[1m]` to a model whose context window is at least
1,000,000. Claude Code reads that suffix to enable its 1M path; the watchdog
strips it before LiteLLM. Native profiles never carry it.

### `groups`: the profile to stay on

`ccp groups` points at the local bridge and exposes three routing groups:

```text
main    opencode-deepseek → MiniMax-M3.1-Flash → omp-deepseek
pro     glm-5.3-flashx → Doubao-Seed-2.1-pro → opencode-deepseek
        → MiniMax-M3.1-Flash → omp-deepseek
flash   beam-free → MiniMax-M3.1-Flash → opencode-deepseek → omp-deepseek
```

LiteLLM fails over inside the request. If one provider runs out of quota, the
conversation continues on the next one; no exit and no new session. The omp
aliases are the last-resort fallback and are reached through the omp gateway.

The group profile advertises the **smallest** context window in its whole
member + fallback chain: a failover must not promise more than the member that
ends up answering can honour. `main` and `flash` carry `[1m]`; `pro` does not,
because qpcmqam caps its chain at 272k.

### Registry, discovery and render

`registry/models.yaml` holds model facts — context, output cap, vision — and is
refreshed from provider APIs:

```bash
ccp discover [provider]   # pull catalogues and measured metadata
ccp render                # regenerate config, clamp/rule maps and profiles
ccp sync                  # copy profile templates into ~/.claude/providers/
```

`registry/profiles.yaml` is the only hand-authored curation layer: tier choices,
aliases, accounts and groups. Generated files are never hand-edited:

| Generated | Purpose |
| :--- | :--- |
| `~/.config/litellm/config.yaml` | LiteLLM deployments, model_info, fallbacks |
| `bridge/clamp_map.json` | model → output cap, read by the watchdog |
| `bridge/request_rules.json` | per-model request rewrites |
| `providers/<name>.json.example` | Claude Code profile with the right context |

`python3 bridge/render_config.py --check` exits non-zero when an output is
stale.

---

## Commands

| Command | What it does |
| :--- | :--- |
| `ccp` | List providers; the active one is starred |
| `ccp <provider> [model]` | Switch provider, optionally to a specific model |
| `ccp groups [main\|pro\|flash]` | Use the routing-group profile |
| `ccp omp <upstream> [model]` | omp-backed provider family (e.g. `ccp omp opencode`) |
| `ccp omp` | List omp upstreams and available accounts |
| `ccp -m <model>` | Hot-switch model inside the current provider |
| `ccp -c` / `ccp -r [N]` | Show active state / roll back N settings backups |
| `ccp restore` | Return to pre-ccp settings; stops ccp services |
| `ccp -t` / `ccp --test` | Quick round-trip through Claude Code |
| `ccp models [provider]` | List models with context, output cap and vision |
| `ccp discover [provider]` | Refresh model metadata from provider APIs |
| `ccp render` / `ccp sync` | Regenerate outputs / install profile templates |
| `ccp ping [provider\|model]` | Probe health, TTFT and token speed |
| `ccp ping --agent` | Time to the first tool call, not the first token |
| `ccp bridge [status\|restart\|start\|stop\|deploy\|logs]` | Manage the local bridge |
| `ccp auth trae` / `ccp auth copilot` | Import a CLI login / Copilot device flow |
| `ccp accounts …` | Extra credentials and manual routing groups |
| `ccp omp …` | omp setup/login/accounts/models/status/sync-token/start/stop/restart |
| `ccp route [--json] [--strict]` | Show each group's chain and the quota evidence |
| `ccp quota [provider]` | What is left on each plan |
| `ccp usage` | Tokens and latency from the bridge's ledger |
| `ccp doctor` | Check litter, duplicate keys, proxy and live answering model |
| `ccp hermes [provider]` | Point the local Hermes agent at a provider's model |
| `ccp -h` / `ccp --help` | Full help |

`ccp ping` rounds: default 2 rounds (~200 output tokens), `-q` 1 round (~64),
`-b` 3 rounds (~1000). `-s` switches to the winner. `--agent` is the useful
number for coding agents: a model can emit a token instantly and still never
call a tool.

---

## The bridge

```text
claude ──> :4000 sse_watchdog ──> :4001 LiteLLM ──> provider /chat/completions
                         │
                         └─ request rewrites: [1m] strip, beta strip,
                            max_tokens clamp, media downgrade, model remap
```

**Watchdog mode (default) is the complete deployment.** LiteLLM sits on `4001`
and the watchdog on `4000`; the watchdog is the only place the request-rewrite
layer exists. `sse_watchdog.py` also closes Anthropic streams after a complete
`message_stop` and drops trailing keepalives.

`bridge/setup_bridge.sh --no-watchdog` puts LiteLLM on `4000` directly. It is
one hop, but it is **lossy**: `[1m]` stripping, the 1M beta strip and the output
clamp all disappear, and real Claude Code traffic on a 1M model can 400. Do not
use it to "fix" a broken bridge.

The bridge also answers the **OpenAI protocol** on `/v1/chat/completions`; see
[docs/openai-api.md](docs/openai-api.md).

### Model remap

A client's model list is a copy of the bridge's. When curation narrows — a
provider reduced to one model, an id renamed — the old name is resolved before
anything else looks at it: if exactly one model of that provider is served, that
is the model being asked for. A provider serving two models is never guessed at,
and a bare group alias (`main`, `pro`) is left alone. Every remap is logged with
both names.

### The loopback never goes through the HTTP proxy

`127.0.0.1:4000/4001/4010` and `:8765/4100` are local. A loopback request that
the HTTP proxy answers for leaves the box, cannot be routed, and comes back
`502` with an empty body — exactly like a dead provider. `bridge/loopback.py`
enforces the bypass in ccp's own tools; the shell still needs an exact entry:

```bash
export no_proxy="127.0.0.1,::1,localhost,$no_proxy"
```

Windows hands WSL a wildcard spelling (`127.*`) that no HTTP stack understands;
`ccp` warns when it sees it.

---

## omp: multi-account login

`ccp` itself reads keys; it does not own provider logins. For the “log in once,
use several accounts” workflow, ccp adapts **omp**:

```bash
ccp omp setup              # install + start omp auth-broker (:8765) and auth-gateway (:4010)
ccp omp login              # `omp auth-broker login`; pick a provider once
ccp omp accounts           # `omp usage`: per-account plan windows
ccp omp                    # list omp upstreams (opencode, anthropic, ...)
ccp omp models opencode    # what that upstream serves
ccp omp opencode           # omp-backed OpenCode, beside `ccp opencode`
ccp omp opencode kimi-k3   # ...or a model inside it
```

**The layer is the provider, not one gateway-wide profile.** `ccp opencode`
(direct ccp provider) and `ccp omp opencode` (omp account pool) are siblings.
The generated profile is `~/.claude/providers/omp-opencode.json`; an omp login
for `anthropic` produces `omp-anthropic.json` the same way.

The omp profile carries only the **gateway bearer token**, never a provider
credential. omp owns OAuth refresh, account rotation and quota. Its sidecar
`omp-*.json.ccp-dynamic` tells `ccp sync` and `ccp doctor` not to reconcile or
archive the profile against the ccp bridge.

Use exact omp ids (`opencode-go/deepseek-v4.1-flash`); the HTTP gateway does not
accept omp CLI role selectors such as `@smol` (verified 404). `pi` is not used
here: it supports one credential per provider, while omp indexes multiple OAuth
accounts.

### omp in the groups

The same gateway is the final fallback in every group:

```text
main/flash  → ... → omp-deepseek-v4.1-flash
pro         → ... → omp-kimi-k3
```

The omp deployments use the gateway's OpenAI chat face with
`Authorization: Bearer`; its Anthropic face rejects LiteLLM's `x-api-key`
(verified 401). `ccp omp setup` / `ccp omp sync-token` mirror the gateway token
into `proxy.env` as `OMP_AUTH_GATEWAY_TOKEN`; run `ccp bridge restart` once to
load it.

---

## Accounts and manual pools

An **account** is another deployment of the same model name, differing only in
which credential it uses. LiteLLM load-balances across accounts and cools one
down when it answers 401/429.

```bash
ccp accounts add <provider> a2 --token <key>
ccp accounts list
ccp accounts remove <provider> a2
ccp accounts group add daily <member> <member> --fallback <name>
ccp render && ccp bridge restart
```

Credentials go to `~/.config/litellm/proxy.env` as
`<PROVIDER>_API_KEY_<SUFFIX>`; only the variable *name* is recorded in
`registry/profiles.yaml`, which is committed.

A **routing group** is one LiteLLM model name with one deployment per
`(member, account)` pair. Claude Code reaches the curated groups through
`ccp groups`, or a custom one through `ccp -m group/<name>`.

---

## Quota, usage and route checks

```bash
ccp quota              # providers that publish a usage API
ccp quota omp          # omp broker: per-account plan windows
ccp usage              # tokens + latency from the watchdog ledger
ccp usage --by-provider
ccp usage --json
ccp route              # group chain + the quota evidence that exists
ccp route --strict     # exit 1 when omp is unreachable or below 10%
```

Only omp publishes quota here; MiniMax does not. `ccp route` therefore marks
the rest as unknown instead of inventing a ranking. `ccp usage` records the
four clocks separately — provider wait, first token, generation tail,
end-to-end — so a sum of generation time cannot be mistaken for total model
time.

---

## Hermes

The local Hermes agent reaches providers through the same bridge:

```bash
ccp hermes                 # what the bot is running now
ccp hermes opencode        # point it at OpenCode's default model
ccp hermes trae deepseek-v4.1-flash
ccp hermes opencode --dry-run
```

Both layouts work: one config per profile under
`~/.hermes/profiles/<name>/config.yaml`, or a single
`~/.hermes/config.yaml`. The command edits only the model keys, preserves
comments, snapshots the profile, and restarts the gateway unit. `--profile`,
`--config`, `--json` and `--dry-run` are available.

---

## Doctor, restore and troubleshooting

```bash
ccp doctor              # backups, duplicate secrets, proxy port, served models
ccp doctor --probe      # send a minimal request; print the model that answered
ccp restore --dry-run   # show the plan
ccp restore --yes       # restore pre-ccp settings; stop ccp services
```

`ccp doctor` fails on:
- more than one `*.bak*` per config directory, or any backup of `proxy.env`
- two environment variables holding the same secret
- an `http_proxy` port that is configured but not listening
- an installed profile whose model the bridge no longer serves

`ccp restore` returns to `settings.json.ccp-original` (or the oldest non-ccp
backup), stops `litellm-ccp`, `ccp-sse-watchdog` and `ccp-trae-bridge`, and
never deletes profiles or keys. Use `--strip` to remove only ccp-injected keys.

---

## Speed

`ccp ping` reports TTFT, decode speed and total time; `--agent` reports the
time to the first tool call. Recent measurements on this machine:

| Provider | Model | TTFT | tok/s | Total |
| :--- | :--- | ---: | ---: | ---: |
| main group | `main` | 1.4 s | 300.5 | 2.0 s |
| trae | `DeepSeek-V4.1-Flash` | 1.9 s | 125.3 | 2.1 s |
| trae (`--agent`) | `DeepSeek-V4.1-Flash` | — | — | 1.0 s to tool call |

A model that thinks before it answers reports its reasoning time separately;
`ccp ping` says when a speed number has no decode window and is therefore not
comparable with the streaming rows.

---

## Credentials and principles

All credentials live in `~/.config/litellm/proxy.env` (mode `0600`), loaded by
the systemd units. `ccp` reads credentials; it does not refresh or rotate a
provider's login unless that provider explicitly delegates to omp. A secrets
file is never rewritten from a failed read and never restored from a backup.

The short version of [PRINCIPLES.md](PRINCIPLES.md):

- one fact, one landing place — model facts in `registry/models.yaml`, choices
  in `registry/profiles.yaml`, secrets in `proxy.env`;
- generated files are never hand-edited;
- failures say which layer, address and key failed;
- fallback is explicit and visible in `ccp doctor` / `ccp route`;
- ccp knows protocols, models and credentials — not business history.

---

## License

MIT. See [LICENSE](LICENSE).
