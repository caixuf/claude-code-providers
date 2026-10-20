# Can Grok Build be proxied? — research note, 2026-10-20

**Status: NOT integrated, and deliberately so.** This file is a record of what was
measured, so the next person (or the next time this comes up) does not have to
re-derive it. `ccp` ships no `grok` provider; nothing in `registry/`, `bridge/`
or `providers/` knows about any of this.

The question was whether xAI's **Grok Build** CLI subscription can back Claude
Code, the way `trae/` backs it from a subscription that publishes no usable API.

**Answer: yes, and more cheaply than expected — the CLI gateway speaks Anthropic
natively, so it needs no bridge at all.** The reasons it is still not wired up
are at the bottom, and they are policy, not technical.

Measured against `~/.grok/bin/grok` v1.0.62 on a logged-in machine
(`~/.grok/auth.json`, OIDC). Every claim below is labeled:

- `observed` — actually executed, with the command or request shape
- `read` — read out of the binary or a config file, not executed

---

## The one finding that decides the design

`https://cli-chat-proxy.grok.com` — the gateway the Grok CLI itself talks to —
**implements the Anthropic Messages API**, not just OpenAI chat completions. So
the `/v1/messages` shape Claude Code speaks is accepted directly.

```
POST https://cli-chat-proxy.grok.com/v1/messages
authorization: Bearer <grok OAuth access token>
x-grok-client-version: 1.0.62      ← required, see below
anthropic-version: 2023-06-01
```

`observed`: a bare call returns `{"type":"message","role":"assistant",...}` and a
streamed one emits the standard Anthropic SSE event sequence.

That matters because it removes the whole LiteLLM hop. `cline/` is bridged
*because* its gateway only speaks OpenAI; this one does not have that problem. By
this repo's own logic (`native: true` when the vendor already speaks Anthropic),
Grok Build would be a native provider exactly like `deepseek/` and `minimax/`.

### What was verified on that endpoint

| Capability | Result |
| :--- | :--- |
| `POST /v1/messages` | `observed` 200, full message object |
| Streaming SSE | `observed` 200, `content_block_delta` … `message_stop`, clean tail |
| `tools` + `tool_use` | `observed` 200, `input_json_delta` and a tool-use block returned |
| `tool_result` round trip | `observed` 200, the model read the result back correctly |
| `thinking` | `observed` 200, `thinking` blocks returned |
| Vision | `observed` 200, a 64×64 PNG read back as "Red" |
| `POST /v1/messages/count_tokens` | `observed` **404** — the endpoint does not exist (Claude Code does not need it) |

Then the real thing, a whole `claude` process against it:

```bash
ANTHROPIC_BASE_URL="https://cli-chat-proxy.grok.com" \
ANTHROPIC_AUTH_TOKEN="<grok OAuth access token>" \
ANTHROPIC_CUSTOM_HEADERS="x-grok-client-version: 1.0.62" \
ANTHROPIC_MODEL="grok-4.7" \
claude -p "Reply with exactly: OK"
# → OK                                                     (observed)
```

and an agent turn with the tool loop in play (`--allowedTools Bash`, running
`hostname`) came back with the machine name — the model called the tool, the
result came back, and it answered from it (`observed`).

So the integration is genuinely `deepseek`-shaped: a profile whose
`ANTHROPIC_BASE_URL` points at the gateway, one extra header, one model id. No
adapter, no translation layer, no `bridge/`.

---

## The version gate: one header, no signature

The gateway refuses a request without a client version and does **not** verify
anything else about the caller. `observed`, on `/v1/messages`:

| `x-grok-client-version` | Response |
| :--- | :--- |
| *(absent)* | `426` — `Your Grok CLI version (none) is outdated. Please update to version 1.0.13 or later` |
| `0.0.1`, `1.0.12` | `426` — same message, naming the version sent |
| `1.0.13`, `1.0.62`, `9.9.99` | `200` |

The floor is **1.0.13**; there is no upper bound and a future version is
accepted. The `User-Agent` does not matter — a request with no UA at all passes
once the header is present. Pinning `x-grok-client-version: 1.0.13` therefore
keeps working as the CLI's real version advances.

This is a version *hint*, not an attestation. Nothing ties the header to a signed
binary, a device, or a session.

---

## Credentials and refresh — the part that decides whether it runs unattended

`read`: `~/.grok/auth.json` holds one entry keyed
`https://auth.x.ai::<client-id>` with `key` (the OAuth access token, a JWT),
`refresh_token`, `expires_at`, `oidc_client_id`, `oidc_issuer`, and the profile
fields. The JWT's own claims carry
`scope: openid profile email offline_access grok-cli:access api:access
conversations:read conversations:write workspaces:read workspaces:write`.

`observed`:

- **Access token lifetime is exactly 6 hours** (`iat` → `exp`; the token endpoint
  also reports `expires_in: 21600`).
- **The refresh token mints new access tokens.**
  `POST https://auth.x.ai/oauth2/token` with
  `grant_type=refresh_token&refresh_token=…&client_id=…` returns a fresh
  `access_token` (plus a `refresh_token`, `expires_in`, `scope`). A token minted
  that way was accepted on `/v1/messages` → `200` (`observed`).
- **The refresh token does not appear to rotate.** Replaying the *same* on-disk
  refresh token twice both succeeded; the returned refresh token differed from
  the disk one but the disk one kept working (`observed`). So a refresh loop
  cannot lock the CLI out — the failure mode that made CodeBuddy's credential
  handling so careful (a rotating token owned by a CLI) does not apply here.

`offline_access` in the scope is the vendor's own signal that this is intended.

> The experiments above used the refresh endpoint **read-only**: nothing was
> written back to `auth.json`. Verified afterwards that the on-disk
> `access_token` and `refresh_token` hashes were unchanged and `grok -p` still
> worked.

---

## Models, context, speed

`observed` on `/v1/messages` — four ids answer `200`:

| id | notes |
| :--- | :--- |
| `grok-4.7` | the CLI's default; the response names the served model `grok-4.7-build` |
| `grok-4.7-build-fast` | served model echoes the same name |
| `grok-4.6` | serves as `grok-4.6-build` |
| `grok-4.5` | serves as `grok-4.5-build` |

`grok-4.7-build` itself answers `404` — the `-build` names are what the server
*reports*, not ids you may request. Do not wire those into a registry.

**Context is 500k, not the 256k the catalogue advertises.** `read`: the gateway's
`/v1/models` entry carries `context_window: 256000` and
`context_windows: [256000, 500000]`. `observed`: a prompt of `251246` tokens
answers `200`; `501246` tokens answers
`400 [input_too_large] The prompt is too long for this model's context window
(501246 tokens > 500000 tokens)`. So `CLAUDE_CODE_MAX_CONTEXT_TOKENS` should be
`500000`, and taking the headline figure at face value would halve the window —
the same class of mistake as Trae's under-reported `prompt_max_tokens`.

Speed through `/v1/messages`, 3 rounds each, identical prompt (~90–500 output
tokens): `grok-4.7` 1.4–1.9 s, `grok-4.7-build-fast` 1.5–1.9 s, `grok-4.5`
3.2–4.8 s, `grok-4.6` 5.9–9.7 s. Only the first two are fast enough to be
pleasant for agent work.

---

## The other door: `api.x.ai` also accepts the subscription token

`observed`, and worth knowing because it is not obvious: the **same OAuth access
token works as a Bearer token on the public API**, no version header, no API key.

- `/v1/chat/completions`, `/v1/responses`, `/v1/models` all `200`
- tools and streaming work
- the rate-limit headers are **identical** to the CLI gateway's
  (`x-ratelimit-limit-tokens: 53000000`, `x-ratelimit-limit-requests: 8300`), which
  suggests one entitlement behind both doors rather than two products
- `/v1/models` lists **14** entries, including the `grok-4.20` family at 1M
  context and the `grok-imagine-*` image/video models — more than the four the
  CLI exposes

A bogus token is rejected on both paths but with *different* errors
(`400 Incorrect API key` on `api.x.ai`, `401 Invalid or expired credentials` on
the CLI gateway), so the two authenticate through separate code paths that happen
to accept the same credential.

This route is OpenAI-shaped, so using it would mean a bridged provider through
LiteLLM. It is strictly worse for this repo's purposes than the native
`/v1/messages` path — and it is the route that looks *more* like an official API
while being the less documented one.

---

## Why it is not wired up

Not a technical limit — all of the above worked. Three reasons, in order of
weight:

1. **It is a consumer subscription posing as an API.** The credential is the
   CLI's own OAuth session, the gate is a version string, and the terms of the
   product are a Grok Build subscription, not API usage. A gateway that starts
   checking a signature, binding a device, or requiring a fresh session breaks
   this overnight, and no amount of code here would help. That is a different
   risk class from `cline/` or `trae/`, which are official (if under-documented)
   API surfaces.
2. **The credential model fights the repo's rules.** The design notes in
   `README.md` and `docs/archived-providers.md` say **read, never refresh** for a
   token a CLI owns, because a second refresher can rotate the token out from
   under the owner. Grok is the opposite shape: 6-hour tokens that *must* be
   refreshed by whoever holds them. Supporting it means adding a self-refreshing
   credential owner, which is the exact thing the CodeBuddy findings argue
   against — even though the non-rotating refresh token makes it safe in this
   particular case.
3. **It was a question, not a need.** The decision at the time was to keep the
   provider set at the five that already work. Measured benefit did not justify a
   sixth, fragile provider plus a new credential lifecycle.

## If it is ever wanted

The cheapest shape, given the measurements above:

1. a `grok` profile, `native: true`, `api_base: https://cli-chat-proxy.grok.com`,
   tiers on `grok-4.7` (and `grok-4.7-build-fast` if a second rung is wanted),
   `CLAUDE_CODE_MAX_CONTEXT_TOKENS: 500000`;
2. `ANTHROPIC_CUSTOM_HEADERS: "x-grok-client-version: 1.0.13"` in the profile —
   note `render_config.render_profile` does **not** emit that key today, so it
   would need a one-line addition (or the value dropped in by `ccp sync`);
3. a `ccp auth grok` that imports `key` from `~/.grok/auth.json` into
   `proxy.env`, and a refresh step against `https://auth.x.ai/oauth2/token` that
   runs before `expires_at` — **read the refresh token, write new tokens back
   atomically**, and never keep a backup of the old one (a revoked token must not
   come back);
4. `ccp discover grok`: the CLI catalogue carries `context_window` and
   `reasoning_effort` but no output cap or vision flag, so it needs `only_models`
   pinned to the four real ids plus `hints` for the facts it omits — the same
   shape as MiniMax, see `docs/archived-providers.md` for that pattern.

Do not trust the pipeline without the two probes that found the surprises here:
`max_tokens` just above and below the real cap, and a prompt just above and below
the advertised context window.
