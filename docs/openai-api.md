# The OpenAI face

The ccp bridge is **one API**, not a Claude Code accessory. It answers the
OpenAI Chat Completions protocol as well as Anthropic Messages, so any
OpenAI-compatible client can use the same local gateway.

Anthropic compatibility exists because Claude Code speaks that dialect. It is a
compatibility layer, not the API.

## Three ways in

| client | base URL | credential | protocol |
| :--- | :--- | :--- | :--- |
| OpenAI client **straight to a relay** | e.g. `https://ai.qpcmqam.cn/v1` | that relay's key | OpenAI |
| OpenAI client **through the ccp bridge** | `http://127.0.0.1:4000/v1` | `sk-ccp-litellm-local` | OpenAI |
| Claude Code | `http://127.0.0.1:4000` | `sk-ccp-litellm-local` | Anthropic |

The first row needs no local process. The second aggregates every provider ccp
knows behind one URL. The third is what Claude Code uses.

The omp gateway is a separate service on `127.0.0.1:4010`; it also speaks both
protocols, but it is reserved for the omp-backed profiles and the omp fallback
aliases (`omp-deepseek-v4.1-flash`, `omp-kimi-k3`).

## Model names

Through the ccp bridge, use the `model_name` entries, i.e. the names
`ccp models` prints:

- `deepseek-flash`
- `MiniMax-M3` / `MiniMax-M3.1-Flash`
- `deepseek-v4.1-flash`
- `opencode-deepseek-v4.1-flash` / `beam-free`
- `trae-deepseek-v4.1-flash` (the Trae adapter itself has no OpenAI face;
  this deployment goes through LiteLLM)
- `qpcmqam-gpt-6.1-sol` / `qpcmqam-gpt-6.1-terra` / `qpcmqam-gemini-4-argon`
- `omp-deepseek-v4.1-flash` / `omp-kimi-k3` (omp fallback)

`GET /v1/models` on `:4000` lists the whole served set, including the `[1m]`
spellings Claude Code may send.

## Examples

```bash
# through the local bridge, any provider
curl http://127.0.0.1:4000/v1/chat/completions \
  -H "authorization: Bearer sk-ccp-litellm-local" \
  -H "content-type: application/json" \
  -d '{"model":"qpcmqam-gpt-6.1-sol","max_tokens":64,
       "messages":[{"role":"user","content":"hi"}]}'

# a bridged Anthropic-native provider
curl http://127.0.0.1:4000/v1/chat/completions \
  -H "authorization: Bearer sk-ccp-litellm-local" \
  -H "content-type: application/json" \
  -d '{"model":"deepseek-flash","max_tokens":64,
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

Any OpenAI-compatible client (opencode, Cursor, Continue, Hermes, a script)
takes the same two values: base URL and credential.

## Cost note

A relay that bills per token may inject a large system prompt into every
request. On the qpcmqam relay, a single-word `gpt-6.1-sol` turn was billed
**13,956 prompt tokens** (13,947 cached), so budget roughly 14k input tokens
even for `hi`. Use the cheap rung for routine work; `pro` is the deliberate
tier and costs more.

## Sharing the bridge

The bridge binds loopback by design, and `bridge_auth.normalize_auth()`
replaces whatever credential the client sends with the bridge's own. Exposing
the port therefore exposes the account's quota. To let somebody else use it,
put an authenticating reverse proxy (Caddy/Nginx basic auth) in front and hand
out **that** URL — never the loopback port.

Credentials live in `~/.config/litellm/proxy.env` (`QPCMQAM_API_KEY`, …). Never
in a profile template, never in a document.
