# Archived providers

These were kept by ccp until the 2026-10-14 prune, when the active set was cut
down to the six that actually worked. Nothing here is loaded any more: the
profiles were moved to `~/.claude/providers_disabled/`, the templates to
`providers/archived/`, and the entries removed from `registry/profiles.yaml`,
`registry/models.yaml` and `discover_models.py`.

To bring one back, either `git revert` the prune commit, or:

1. move its template back from `providers/archived/` into `providers/`
2. re-add its profile + aliases to `registry/profiles.yaml`
3. re-add its entry to `PROVIDERS` in `bridge/discover_models.py`
4. `ccp discover <provider> && ccp render && ccp bridge deploy`

The findings below are kept because they were expensive to establish and are not
obvious from the code.

---

## CommandCode (`cmdc`)

Four profiles (`cmdc-deepseek`, `-fast`, `-pro`, `cmdc-vision`) on one model
ladder, all bridged through LiteLLM at `https://api.commandcode.ai/provider/v1`.
87 catalogued models, every one of them capped at 393216 output — verified by
probe (`max_tokens=393216` → 200, `393217` → 400), which is why that number was
a hard-coded constant rather than something discovered.

**Archived because the account ran out of credits.** Every profile answered
`You have insufficient credits to make this request`; the configuration itself
was sound. Top the balance up and the revert restores it unchanged.

---

## CodeBuddy (Tencent coding-copilot)

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


---

## GitHub Copilot

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

