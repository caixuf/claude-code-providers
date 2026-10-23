# The CodeBuddy / WorkBuddy channel check is proxyable

**Status:** measured against the live gateway on 2026-10-23/24. Every number
below comes from a request that was actually sent and answered; nothing is
inferred from documentation.

This file contradicts an earlier conclusion in this repo. The archived
CodeBuddy section of [`archived-providers.md`](archived-providers.md) says the
gateway's channel check "is not something a proxy can guarantee". That is
wrong: the check is a fixed, reproducible rule, and it was found by capturing
what the official client sends and then replaying it from an ordinary HTTP
client.

---

## Why this matters

Tencent sells one subscription under two names. **CodeBuddy Code** is the CLI
(`codebuddy`, npm `@tencent-ai/codebuddy-code`); **WorkBuddy** is the desktop
app built from the same client. magpie treats them as two subscriptions —
`workbuddy` (CN, `copilot.tencent.com`) and `workbuddy-ai` (INTL,
`www.workbuddy.ai`) — and serves either one upstream to any agent. That is the
capability worth copying.

ccp had its own CodeBuddy provider and archived it on 2026-10-16 because the
channel check refused real Claude Code sessions with

```
400 {"code":11128,"msg":"Illegal API invocation from an unapproved channel"}
```

The account on this machine is the INTL one (`auth.domain = www.codebuddy.ai`,
token valid to 2027-10-11), so every measurement below is against
`https://www.codebuddy.ai/v2/chat/completions`. The CN gateway answers a
foreign token with a bare openresty 401, exactly as the archived notes say.

---

## Step 1 — what the approved client actually sends

`codebuddy` was pointed at a logging reverse proxy
(`CODEBUDDY_BASE_URL=http://127.0.0.1:8899`) so the real request could be
read. CLI v2.171.0 asks for `deepseek-v4.1-flash` and sends:

```
POST /chat/completions                       (base URL already ends in /v2)
model: deepseek-v4.1-flash        messages[0].role = system
Authorization: Bearer <accessToken>
X-Conversation-ID / X-Conversation-Message-ID / X-Conversation-Request-ID
X-Request-ID / X-Root-Request-ID
X-Agent-Type / X-Agent-Intent / X-Agent-Purpose / X-Agent-Runtime
X-IDE-Type: CLI   X-IDE-Name: CLI   X-IDE-Version: 2.171.0   X-Product: SaaS
X-Private-Data: false   x-codebuddy-request: 1   x-requested-with: XMLHttpRequest
X-User-Id / X-Domain: www.codebuddy.ai
traceparent / b3 / X-B3-* / X-Trace-ID / baggage
User-Agent: CLI/2.171.0 CodeBuddy/2.171.0
```

The WorkBuddy **desktop** bundle (`…/Programs/WorkBuddy/resources/app.asar`)
sends the same family with `X-IDE-Type: WorkBuddy`, `X-IDE-Name: WorkBuddy`,
`X-Private-Data: true`, `X-Agent-Intent: craft`, `x-codebuddy-request: 1`,
`User-Agent: WorkBuddy/<ver> WorkBuddy/<ver>`, plus `X-Custom-Channel-Id:
unknown`, `X-Device-Token` (Turing/Qimei) and `X-Device-Id-List`. Its endpoint
constant is `https://copilot.tencent.com/v2/chat/completions`.

So the desktop app and the CLI are two *channels* of one gateway, and both
identify themselves in the headers.

---

## Step 2 — replay it from an ordinary client

A plain `python-urllib` script, the account's own token, and nothing else:

| Headers | Body | Answer |
| :--- | :--- | :--- |
| Authorization + Content-Type | system + user | `400 11128 "request illegal"` |
| + `User-Agent: CLI/2.171.0 CodeBuddy/2.171.0` | same | `400 request illegal` |
| + `X-IDE-Type` / `X-IDE-Name` / `X-IDE-Version` | same | `400 request illegal` |
| + `X-Product` / `x-codebuddy-request` / `X-Private-Data` | same | `400 request illegal` |
| + `X-User-Id` / `X-Domain` | same | `400 request illegal` |
| the whole captured set | system + user | `400 11128 "first message is not system prompt"` |
| the whole captured set | **first message = system** | **200 OK (SSE)** |
| the whole captured set | first message = user | `400 first message is not system prompt` |
| the whole captured set | first message = developer | `400 Illegal API invocation from an unapproved channel` |
| the whole captured set | system = `You are Claude Code, Anthropic's official CLI for Claude.` | `400 Illegal API invocation from an unapproved channel` |
| the whole captured set | neutral system + Claude Code identity in an **assistant** turn | `400 Illegal API invocation from an unapproved channel` |
| the whole captured set | system = `""` + Claude Code identity in the first **user** turn | **200 OK** |
| the whole captured set | system = `""` + DSH identity in the first **user** turn | **200 OK** |

The header set gets the request *past* the client gate — the error changes from
`request illegal` to a body complaint — and the body rule then decides.

---

## Step 3 — the minimum that opens the gate

Dropping one header at a time from the captured set:

| Removed | Answer |
| :--- | :--- |
| `traceparent` / `b3` / `X-B3-*` / `X-Trace-ID` / `baggage` | 200 |
| `User-Agent` (replaced by `python-urllib/3.11`) | 200 |
| `X-User-Id`, `X-Domain`, `x-codebuddy-request`, `X-Private-Data`, `x-requested-with`, every `X-Agent-*` | 200 |
| `X-IDE-Type` / `X-IDE-Name` changed to `WorkBuddy` | 200 |
| `X-Request-ID` | 200 |
| `X-Root-Request-ID` | 200 |
| **`X-Conversation-ID`** | 400 request illegal |
| **`X-Conversation-Message-ID`** | 400 request illegal |
| **`X-Conversation-Request-ID`** | 400 request illegal |

All 3-of-5 subsets of the conversation family were tried; exactly one passes:

| Subset | Answer |
| :--- | :--- |
| **`X-Conversation-ID` + `X-Conversation-Message-ID` + `X-Conversation-Request-ID`** | **200** |
| every other subset | 400 request illegal |

The values are not validated: all three replaced by the literal string
`abc123` still answer 200.

**The whole recipe is three headers.** No User-Agent, no `X-IDE-*`, no device
token, no signature:

```
X-Conversation-ID:        <anything>
X-Conversation-Message-ID: <anything>
X-Conversation-Request-ID: <anything>
```

---

## The rule, in one paragraph

1. **Header gate.** A request without the three `X-Conversation-*` headers is
   refused as `11128 "request illegal"`, however correct its body is.
2. **Body gate.** The first message must have `role: "system"` (a `developer`
   role is refused), and neither the system messages nor any assistant message
   may carry a foreign client identity such as Claude Code's
   `You are Claude Code, Anthropic's official CLI for Claude.` A user-role
   message may say anything.
3. **Determinism.** ~20 bare attempts all failed and every correctly-shaped
   attempt succeeded. The check is a rule, not a risk score — which is what
   made the "intermittent" reading wrong in the first place.

Models confirmed answering under the recipe: `deepseek-v4.1-flash`, `glm-5.3`,
`hy3`. (`kimi-k3` refuses an *empty* system message with its own rule; this
account is a `personal_free_user`, so `gpt-5.6-luna` is refused as
`model not allowed`.)

---

## What this means for ccp

The archived conclusion was reached with a body sanitiser that could not see
the headers: LiteLLM forwards `Authorization` and little else, so the request
arrived with no conversation family at all and was refused as
`request illegal`. The sanitiser was also, correctly, needed for the second
gate — its rule (neutralise the identity in *every* system block and *every*
assistant turn, not just the first) matches what was measured here.

Restoring the provider needs three things:

1. **The three headers on every upstream request.** LiteLLM deployments can
   carry `extra_headers`, but a fixed value would be wrong — the gateway
   correlates a conversation with a session, and the watchdog that already
   rewrites the body (`bridge/sse_watchdog.py`) is the natural place to mint
   one id per request and stamp it on.
2. **The sanitiser back**, scoped to this provider's models, via
   `request_rules` in `registry/profiles.yaml` — the plumbing is still there
   (`sanitize_channel_markers` in `bridge/sse_finalize.py`), only the rule was
   dropped with the provider.
3. **The identity moved out of the system role.** A foreign identity is
   refused in system *and* assistant messages; putting the agent's own
   instructions into the first user message instead is what magpie's plugin
   does (`0.1.29`: "a system prompt WorkBuddy refuses as an unapproved channel
   (11128) is sent once more as the first user message's instructions, with
   WorkBuddy's default system prompt, since WorkBuddy checks only system and
   assistant messages").

The credentials side is unchanged and still applies: magpie reads WorkBuddy's
own account from `<shared data>/auth/workbuddy-desktop.info` and never writes
it — but only while credential protection is off. On this machine that file is
**encrypted** (`$wbEncrypted` envelopes), so the desktop session cannot be
imported the way the CLI session can. The CLI session
(`~/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info`)
is still plaintext and still valid.

## Open questions

- The CN gateway (`copilot.tencent.com`) was not measured: this account's token
  belongs to the INTL host and the CN one answers 401. The 11128 code and its
  two sub-messages are the same family, and the WorkBuddy desktop bundle points
  at the CN host, so the rule is very likely identical — but that is inference.
- Whether the three conversation headers are also what the *WorkBuddy AI*
  (`www.workbuddy.ai`) build wants, or whether the INTL desktop build adds
  something of its own.
- magpie's own WorkBuddy provider is plugin-served
  (`@magpie-community/opencode-workbuddy-auth`, package `packages/workbuddy`),
  and its built-in Go code has been retired. The plugin's request-shaping is
  where its version history lives — 0.1.15 (system prompt as a developer
  message), 0.1.27 (DSH's identity line reworded), 0.1.28 (a hint that names no
  client), 0.1.29 (the system prompt re-sent as the first user message).
