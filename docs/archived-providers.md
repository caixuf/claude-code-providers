# Archived providers

These were kept by ccp until it pruned them. The 2026-10-14 cut took the active
set down to what actually worked; **Baidu Qianfan was archived on 2026-10-20**
and **restored on 2026-10-24** (the plan was bought again and wanted back — see
its section below), while **MiniMax was narrowed to two models the same day** —
`MiniMax-M3` and `MiniMax-M3.1-Flash` — which is a curation cut, not an
archive: its folder stays, the M2.x rungs are gone from the registry. The active
set is six folders now: `deepseek/`, `minimax/`, `opendesign/`, `cline/`,
`qianfan/`, `trae/`.

Nothing here is loaded any more: each archived provider's profile was moved to
`~/.claude/providers_disabled/`, its template to `providers/archived/`, and its
entries removed from `registry/profiles.yaml`, `registry/models.yaml` and
`discover_models.py`. Qianfan is the exception right now: its template sits in
`providers/` and its entries are live again.

**CodeBuddy was reinstated on 2026-10-16 and archived again the same day.** It
was believed that the gateway's channel check refused real Claude Code
sessions with `11128 Illegal API invocation from an unapproved channel` and
that "it is not something a proxy can guarantee". That belief was wrong: the
check is a fixed rule, and the requests were refused because they carried none
of the gateway's conversation headers. The rule, the capture of the official
client and the three-header recipe are in
[`workbuddy-channel-check.md`](workbuddy-channel-check.md).

**OpenCode Zen and Antigravity (`agy`) were archived on 2026-10-16** — not
because they broke, but because they were not worth the surface. Unlike every
other entry here they are *local adapters*, not gateways, so archiving one means
moving more than a template: its adapter, its systemd unit example and its tests
went to `bridge/archived/`. Both remain fully reversible; their findings are at
the bottom of this file.

To bring one back, either `git revert` the prune commit, or:

1. move its template back from `providers/archived/` into `providers/`
2. re-add its profile + aliases to `registry/profiles.yaml`
3. re-add its entry to `PROVIDERS` in `bridge/discover_models.py` — and, where it
   had a normalizer of its own (Qianfan had one: `norm_qianfan` plus its
   `NORMALIZERS` entry), re-add that too. The local adapters below never had a
   `PROVIDERS` entry at all, because the adapter owns its own catalogue
4. `ccp discover <provider> && ccp render && ccp bridge deploy`

Anything re-added also needs its key or token put back in
`~/.config/litellm/proxy.env` before step 4 can succeed.

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

## Baidu Qianfan (百度千帆 Token Plan) — archived 2026-10-20, restored 2026-10-24

**Archived on 2026-10-20 because the plan was no longer used**, not because
anything broke. It is a normal bridged provider: plain OpenAI Chat Completions
through LiteLLM, `openai/` prefix, `QIANFAN_API_KEY` in
`~/.config/litellm/proxy.env`. Nothing about it was fragile — the six ids the
old subscription served all answered, and at the time it held both the fastest
first token and the shortest total time of any provider here (MiniMax-M3.1-Flash
has since taken both).

Two Token-Plan properties were established by probing, and both are worth
knowing before restoring it:

- **The key is only accepted on the Token-Plan prefix.** `api_base` is pinned to
  `https://qianfan.baidubce.com/v2/tokenplan/personal`; the same key against plain
  `/v2/chat/completions` answers `token_plan_person_api_key_not_allowed`.
- **The catalogue is far wider than the plan.** `/v2/models` lists ~35 ids, but
  Token Plan Person serves only the GLM and DeepSeek families — the ERNIE,
  Qwen, VL, OCR and embedding ids all answer HTTP 403
  `token_plan_person_model_not_supported`.

**The supported set belongs to the subscription, not to the provider.** The
2026-10-24 key serves nine ids where the 2026-10-20 one served six —
`deepseek-v4.1-flash-1017`, `deepseek-v4-pro-0813` and `glm-5.1` are new. So
`only_models` is re-derived per key by `bridge/qianfan_probe.py`, which walks
the whole catalogue against `QIANFAN_API_KEY` and prints the allow-list to
paste into `bridge/discover_models.py`. A catalogue entry is a claim, not an
entitlement: the probe is the only evidence.

Its figures: `deepseek-v4.1-flash` at 1.44M context and genuinely multimodal, and
first token in 759–957 ms across the three DeepSeek rungs — against 1.5 s for the
next best at the time, which matters because that latency is paid on every turn.
It was also the anchor member of the `main`/`pro`/`flash` routing groups in
`registry/profiles.yaml`, which had to be re-pointed when it went: the groups
survived (failover is still worth having), but `main` and `pro` now lead with a
`cline` model and fall back to `opendesign`, while `flash` leads with
`opendesign-glm-5.3-flash` and falls back to `cline-glm-5.3` — ClinePass carries
no flash variant, so the roles inverted there.

Restoring it is the four steps above, with the qianfan-specific parts spelled out:

1. move `providers/archived/qianfan.json.example` back to `providers/`
2. re-add its profile and its `qianfan-*` aliases to `registry/profiles.yaml`
   (the three routing groups still exist and can take it back as the primary
   member; check each group's fallback as you go, since `flash` now falls back to
   `cline-glm-5.3` rather than to an `opendesign` model)
3. re-add the `qianfan` entry to `PROVIDERS` **and** `norm_qianfan` plus its
   `NORMALIZERS` entry in `bridge/discover_models.py` — its catalogue carries
   real `context_length` / `max_completions_tokens` /
   `architecture.input_modalities` metadata, which is why it has a normalizer of
   its own rather than the generic one
4. put the key back in `proxy.env` as `QIANFAN_API_KEY`, then
   `ccp discover qianfan && ccp render && ccp bridge deploy`

> **Restored on 2026-10-24, on a new subscription.** All four steps above were
> done again: the template moved back to `providers/qianfan.json.example`, the
> profile and the nine `qianfan-*` aliases are back in `registry/profiles.yaml`,
> `PROVIDERS` / `norm_qianfan` / `NORMALIZERS` are back in
> `bridge/discover_models.py`, and the `main` / `pro` / `flash` groups lead with
> Qianfan again (it was their anchor member before the 2026-10-20 archive).
>
> **The old key was out of credits** — every model answered
> `429 {"code":"credit_quota_exceeded","message":"Token Plan Person monthly quota
> limit exceeded"}` — so the plan was bought again and `QIANFAN_API_KEY` in
> `proxy.env` now holds the new key. All nine models answer on it, measured
> through the bridge (`:4000`, the path Claude Code actually uses):
> `deepseek-v4.1-flash` 833 ms, `deepseek-v4.1-flash-1017` 400 ms,
> `deepseek-v4-pro` 712 ms, `deepseek-v4-pro-0813` 951 ms,
> `deepseek-v4-flash-0731` 999 ms, `glm-5.3` 906 ms, `glm-5.3-flash` 1834 ms,
> `glm-5.2` 815 ms, `glm-5.1` 711 ms — each returning its own id and a real
> completion, not an error. `ccp ping` puts `deepseek-v4.1-flash-1017` first on
> TTFT (460 ms).
>
> **Two operational notes.** The key lives in `proxy.env`, which the systemd
> unit loads as `EnvironmentFile` — a new key only reaches the bridge after
> `systemctl --user restart litellm-cmdc.service`, not after a `ccp render`.
> And the catalogue endpoint answers `429 Over rate limit` when probed in a
> burst, so the probe retries with a backoff instead of concluding that the
> plan is gone.

---

## CodeBuddy (Tencent coding-copilot) — archived 2026-10-16

[CodeBuddy](https://www.codebuddy.ai/cli) (Tencent coding-copilot) is a flat
subscription that fronts a whole fleet of third-party models — DeepSeek, GLM,
Kimi, GPT, Gemini, Hunyuan. It has **no API-key product**: the gateway
authenticates the session the CodeBuddy Code CLI already holds, so `ccp`
imports that session.

> Archived: the template, the importer (`bridge/archived/codebuddy_auth.py`) and
> its tests are under `archived/`; the registry entries and the `ccp auth
> codebuddy` command are gone. The section below is the operator's reference.

### Why it is out: the channel check was believed to be unproxyable

> **Corrected on 2026-10-24.** The channel check *is* proxyable: the refusal was
> caused by the request carrying none of the gateway's conversation headers, not
> by the body. The full rule, the capture of the official client and the
> three-header recipe are in
> [`workbuddy-channel-check.md`](workbuddy-channel-check.md). The diagnosis
> below is kept because the sanitiser bug it found is still real and still
> needed — but the conclusion "a proxy cannot guarantee it" does not hold.

Two things had to be true for it to work, and only the first was fixable:

1. **The two gateways.** The account signs into the CN site
   (`copilot.tencent.com`, behind `www.codebuddy.cn`), and ccp hard-coded the
   INTL one (`www.codebuddy.ai`), which answers a foreign token with a bare
   `401 invalid_token` — indistinguishable from an expired login. Deriving the
   host from the login's own `auth.domain` fixed that; the token was never bad.
2. **The channel check.** Even with the host right and the body sanitised, real
   Claude Code sessions are refused with
   `400 {"code":11128,"msg":"Illegal API invocation from an unapproved channel"}`
   — *intermittently*: the same `claude -p` invocation both succeeded and failed
   minutes apart, and a captured failing body, replayed to the gateway with the
   upstream model id, was **accepted**. So the trigger is not the body content,
   and a proxy that only rewrites the body cannot guarantee it. This is the
   "official client" the message asks for.

Along the way the sanitizer itself turned out to have a real bug, which *is*
fixed and kept (`bridge/sse_finalize.py`): the identity line was neutralised
only when it opened the **first** system block, while the gateway inspects
**every** system block and **every assistant turn**. Identity in a later block,
or in a past assistant message of a resumed conversation, was refused — verified
against the live gateway before the fix. Whoever restores this provider starts
from the corrected rule.

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
`GET /v2/enterprises/personal/models` on the account's own gateway, which is
**account-scoped**: `ccp models codebuddy` shows exactly what your plan exposes.
The catalogue is wider than it is useful, so `discover_models.py` narrows it with
`only_models` and every surviving id is aliased.

**Two gateways, one login.** The international site (`www.codebuddy.ai`) and the
CN one (`copilot.tencent.com`) do not share tokens, and the wrong host answers a
bare `401 invalid_token` — indistinguishable from an expired login, which is what
the 2026-10-14 prune mistook it for. The host is now derived from the login's
`auth.domain` (`api_base()` in `bridge/codebuddy_auth.py`), so nothing has to be
hand-edited per account; `CODEBUDDY_API_BASE` overrides it. The fleets differ —
the CN plan carries no GPT or Gemini — so `only_models` and the aliases are
per-deployment choices, not one global list.

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


## OpenCode Zen free tier (`opencode`)

Archived 2026-10-16 (adapter + unit + tests now in `bridge/archived/`, template
and profile/aliases removed). It worked; it was cut for surface area.

opencode publishes `deepseek-v4.1-flash-free` and friends at $0, but the free
tier is gated to the opencode client itself: a direct call to
`zen/v1/chat/completions` answers `403 FreeTierError` whatever the credentials,
and a forged `User-Agent: opencode/…` plus `x-opencode-session` is not enough.
`opencode_bridge.py` therefore drove the real local opencode service
(`~/.local/state/opencode/service.json` holds its URL and password) and
translated its session API into Anthropic Messages on `:4110`. No spoofed
headers, no key.

Two findings worth keeping:

* **The per-turn overhead was the tool table, not the prompt.** A 44-character
  turn carried ~6083 tokens of context nobody asked for. Replacing the system
  text moved it 6083 → 5780, so the prompt was never the bulk — the definitions
  of the service's ~60 native tools were, injected every turn. Denying those
  through the session's own `permissions` took the same turn to ~1963, and
  ~2553 end to end through the bridge.
* **`read` must stay reachable.** The ruleset opened with `allow *` then denied
  each tool; denying `read` as well (or a blanket `deny *`) answered
  `403 FreeTierError` instead of running the turn.

The free-tier id list churns, and the registry drifted from it — measured
2026-10-16, the gateway's own list was:

```
big-pickle, cat-jam-free, deepseek-v4.1-flash-free, longcat-2.5-preview-free,
mimo-v2.6-flash-free, muse-spark-1.3-contributor-free, nemotron-3-ultra-free,
nemotron-3.5-lightning-free, nemotron-3.5-super-vl-free, space-bunny-free
```

`step-5-preview-free` (long in the registry) is no longer on it and answers
`unknown model`. If this is ever restored, re-read that list rather than
reusing the old one. Reversal: move the adapter back from `bridge/archived/`,
re-add the `opencode` profile + aliases, and re-add its block to
`registry/models.yaml` (it was never in `discover_models.py`'s `PROVIDERS` —
the adapter owns its own catalogue).

---

## Antigravity CLI (`agy`)

Archived 2026-10-16 (adapter + unit + tests in `bridge/archived/`). Like
opencode: it worked, it was cut for surface area.

`agy_bridge.py` drove the `agy` CLI (`--input-format stream-json`) and served
Anthropic Messages on `:4120`. Six models on one Google account, including Claude
Sonnet/Opus 4.6 — the only route to Anthropic's own models here without an
Anthropic key. That remains its one irreplaceable quality if it is ever wanted
back.

**The wire protocol cannot carry tool schemas** — a limit of the protocol, not of
the adapter. Measured: `event=system`, `event=tools` and `event=config` are each
rejected with `warning: ignoring unsupported stream input message event`, and a
top-level `tools` field on the user event is accepted without complaint but
**silently ignored** (the model answered that the named tool did not exist, for a
tool sent exactly that way). agy's only input is a user message, so the model
never sees anything but agy's own 60-tool table; Claude Code's tools can only be
serialised into the prompt as text. Text reasoning was reliable; tool use was
best-effort by construction.

**Two measured limits, both load-bearing.** agy caps input at ~63.5k tokens
(gemini) / ~72.9k (claude-sonnet), about 13k of which is its own system prompt;
past the cap the input is truncated *silently*, so the registry's `ctx` was the
usable budget rather than the model's window.

Use the **WSL-native** binary (`~/.local/bin/agy`). A Windows `agy.exe` exists
under the user profile and is not interchangeable: it re-runs the Google OAuth
flow on every new process and floods the desktop with login prompts. The adapter
took an absolute path and never resolved through `PATH`, where the Windows copy
comes first.
