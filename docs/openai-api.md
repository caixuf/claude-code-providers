# The OpenAI face

The bridge is **one API**, not a Claude Code accessory. It answers the OpenAI
protocol as well as the Anthropic one, so a client that already speaks OpenAI
needs nothing installed: no litellm, no ccp, no Anthropic shim.

Anthropic compatibility exists for exactly one reason — Claude Code only speaks
that dialect. It is a compatibility layer, not the API.

## Three ways in

| client | base URL | credential | protocol |
| :-- | :-- | :-- | :-- |
| OpenAI client **straight to the relay** | `https://ai.qpcmqam.cn/v1` | the relay key | OpenAI |
| OpenAI client **through the local bridge** | `http://127.0.0.1:4000/v1` | `sk-ccp-litellm-local` | OpenAI |
| Claude Code | `http://127.0.0.1:4000` | `sk-ccp-litellm-local` | Anthropic |

The first row needs no local process at all. The second aggregates every
provider ccp knows (OpenAI-native and Anthropic-native alike) behind one URL.
The third exists only for Claude Code.

## Verified on 2026-11-03

```
POST https://ai.qpcmqam.cn/v1/chat/completions          → 200  ("ok")
POST http://127.0.0.1:4000/v1/chat/completions          → 200  ("ok")   model=qpcmqam-gpt-6.1-sol
POST http://127.0.0.1:4000/v1/chat/completions          → 200  ("ok")   model=deepseek-flash
```

Both an OpenAI-native provider (qpcmqam) and an Anthropic-native one
(deepseek) answer on the OpenAI face, which is what makes the bridge usable as
a general API rather than a Claude Code adapter.

## Model names

- **Direct to the relay** — the upstream ids: `gpt-6.1-sol`, `gpt-6.1-terra`,
  `gemini-4-argon`.
- **Through the bridge** — the `model_name` entries, i.e. the names
  `ccp models` prints: `qpcmqam-gpt-6.1-sol`, `deepseek-flash`,
  `qianfan-deepseek-v4.1-flash-1017`, `MiniMax-M3.1-Flash`, …
  `GET /v1/models` on the bridge lists them all.
- `trae` has **no OpenAI face**: its adapter speaks Anthropic only, on
  `:4100`.

## Examples

```bash
# straight to the relay, nothing local
curl https://ai.qpcmqam.cn/v1/chat/completions \
  -H "authorization: Bearer $QPCMQAM_API_KEY" \
  -H "content-type: application/json" \
  -d '{"model":"gpt-6.1-sol","max_tokens":64,
       "messages":[{"role":"user","content":"hi"}]}'

# through the local bridge, any provider
curl http://127.0.0.1:4000/v1/chat/completions \
  -H "authorization: Bearer sk-ccp-litellm-local" \
  -H "content-type: application/json" \
  -d '{"model":"qpcmqam-gpt-6.1-sol","max_tokens":64,
       "messages":[{"role":"user","content":"hi"}]}'
```

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:4000/v1",
    api_key="sk-ccp-litellm-local",
)
print(client.chat.completions.create(
    model="deepseek-flash",
    messages=[{"role": "user", "content": "hi"}],
).choices[0].message.content)
```

Any OpenAI-compatible client (opencode, Cursor, Continue, hermes, a script)
takes the same two values: base URL and credential.

## Cost note: the relay bills a floor

A single-word turn against `gpt-6.1-sol` was billed **13,956 prompt tokens**
(13,947 of them cached). The relay injects a large system prompt into every
GPT request, so budget ~14k input tokens per call even for `hi`. Put the cheap
work on `gpt-6.1-sol`; `gpt-6.1-terra` is the deliberate tier and costs more.

## Sharing the bridge

The bridge binds loopback by design, and `bridge_auth.normalize_auth()`
replaces whatever credential the client sends with the bridge's own. Exposing
the port therefore exposes the account's quota. To let somebody else use it,
put an authenticating reverse proxy (Caddy / Nginx basic auth) in front and
give out **that** URL — do not hand out the loopback port.

Credentials live in `~/.config/litellm/proxy.env` (`QPCMQAM_API_KEY`, …). Never
in this file, and never in a profile template.
