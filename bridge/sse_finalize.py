"""Forward Anthropic SSE; stop extra frames after a complete message_stop.

Do **not** inject synthetic JSON into a live stream. LiteLLM treats that as
``JSON error injected into SSE stream`` / MidStreamFallbackError and Claude
Code retries for many minutes (API_TIMEOUT_MS).

Idle cut is disabled by default (``ping_idle_s=0``). Long tool/Explore turns
can go minutes without a token; a 12s idle inject aborted LiteLLM mid-JSON.
"""

from __future__ import annotations

MESSAGE_STOP_MARK = b"event: message_stop"


def sse_should_close(buf: bytes) -> bool:
    return MESSAGE_STOP_MARK in buf


def split_complete_events(buf: bytes) -> tuple[list[bytes], bytes]:
    events: list[bytes] = []
    while True:
        idx_lf = buf.find(b"\n\n")
        idx_crlf = buf.find(b"\r\n\r\n")
        if idx_lf == -1 and idx_crlf == -1:
            break
        if idx_crlf != -1 and (idx_lf == -1 or idx_crlf < idx_lf):
            part = buf[: idx_crlf + 4]
            buf = buf[idx_crlf + 4 :]
        else:
            part = buf[: idx_lf + 2]
            buf = buf[idx_lf + 2 :]
        events.append(part)
    return events, buf


def event_is_ping(ev: bytes) -> bool:
    lines = [ln.strip() for ln in ev.splitlines() if ln.startswith(b"event:")]
    return bool(lines) and all(ln == b"event: ping" for ln in lines)


class SseCutState:
    def __init__(self, *, ping_idle_s: float = 0.0, first_byte_s: float = 90.0) -> None:
        self.ping_idle_s = ping_idle_s
        self.first_byte_s = first_byte_s
        self.saw_real = False
        self.saw_stop = False
        self.buf = b""

    def wait_budget(self) -> float:
        if not self.saw_real:
            return self.first_byte_s
        if self.ping_idle_s and self.ping_idle_s > 0:
            return self.ping_idle_s
        return 3600.0

    def feed(self, chunk: bytes) -> tuple[bytes, bool]:
        """Return (bytes_to_forward, should_stop_forwarding)."""
        if not chunk:
            return b"", True
        self.buf += chunk
        events, self.buf = split_complete_events(self.buf)
        out = b""
        for ev in events:
            out += ev
            if sse_should_close(ev):
                self.saw_stop = True
                return out, True
            if not event_is_ping(ev):
                self.saw_real = True
        return out, False

    def idle_timeout(self) -> bytes:
        return b""


def strip_1m_model(body: bytes) -> tuple[bytes, str | None]:
    """Strip [1m] or [1M] suffix from request body model field for upstream compatibility."""
    if not body or (b"[1m]" not in body and b"[1M]" not in body):
        return body, None
    try:
        import json as _j
        _b = _j.loads(body)
        if isinstance(_b, dict) and isinstance(_b.get("model"), str) and _b["model"].lower().endswith("[1m]"):
            orig_m = _b["model"]
            _b["model"] = orig_m[:-4]
            return _j.dumps(_b).encode("utf-8"), orig_m
    except Exception:
        pass
    return body, None


ONE_M_BETA = "context-1m-2025-08-07"


def strip_1m_beta(header_value: str) -> str:
    """Remove the 1M-context beta token from an ``anthropic-beta`` header value.

    Claude Code adds ``context-1m-2025-08-07`` whenever a model carries the
    ``[1m]`` marker. That beta is meaningful only to Anthropic's own API; the
    OpenAI-compatible gateways behind the bridge ignore it, and a strict
    gateway can 400 on an unknown beta. Dropping it makes 1M models just work.
    Returns the remaining comma-separated tokens ('' if none left).
    """
    if not header_value:
        return header_value
    keep = [
        p.strip()
        for p in header_value.split(",")
        if p.strip() and p.strip().lower() != ONE_M_BETA
    ]
    return ",".join(keep)


def norm_model_key(name: str) -> str:
    """Normalize a model name for rule-map lookup: strip any [..] tier suffix, lower-case."""
    if not isinstance(name, str):
        return ""
    base = name.split("[", 1)[0].strip()
    return base.lower()


# Back-compat alias: this helper predates the rule maps that now share it.
_norm_model = norm_model_key


# Claude Code injects a client-attribution block as the first system block:
#
#   x-anthropic-billing-header: cc_version=2.1.287.95e; cc_entrypoint=sdk-cli;
#
# Tencent's CodeBuddy gateway rejects any request carrying that marker with
# ``400 {"code":11128,"msg":"Illegal API invocation from an unapproved channel"}``
# — it is a channel check, not an auth failure, and the whole request dies
# (verified against the live gateway: the identical 71 KB tool-calling payload
# passes once this one line is removed). Anthropic's own API needs the header,
# so the rewrite is applied per-model via ``bridge/request_rules.json`` and
# never to the native Anthropic profiles.
BILLING_HEADER_MARKER = "x-anthropic-billing-header:"


def _strip_marker_lines(text: str) -> tuple[str, bool]:
    """Drop every line of ``text`` that declares the billing header."""
    if BILLING_HEADER_MARKER not in text.lower():
        return text, False
    kept = [
        ln
        for ln in text.splitlines()
        if not ln.strip().lower().startswith(BILLING_HEADER_MARKER)
    ]
    out = "\n".join(kept)
    # preserve a trailing newline if the original had one and we removed the tail
    if text.endswith("\n") and not out.endswith("\n"):
        out += "\n"
    return out, out != text


def _clean_content(content):
    """Return ``(content, changed)`` with billing-header blocks/lines removed.

    Handles both Anthropic shapes: a bare string, or a list of content blocks
    (``{"type": "text", "text": ...}`` and friends). Non-text blocks are left
    untouched.
    """
    if isinstance(content, str):
        return _strip_marker_lines(content)
    if not isinstance(content, list):
        return content, False
    changed = False
    blocks = []
    for blk in content:
        if isinstance(blk, dict) and isinstance(blk.get("text"), str):
            text, hit = _strip_marker_lines(blk["text"])
            if hit:
                changed = True
                blk = dict(blk, text=text)
                # A block that held nothing but the marker is noise upstream;
                # keep it only if it still carries content.
                if not text.strip():
                    continue
        blocks.append(blk)
    return blocks, changed


def strip_billing_header(body: bytes) -> tuple[bytes, bool]:
    """Remove Claude Code's ``x-anthropic-billing-header`` marker from a request.

    Returns ``(body, changed)``. The marker lives in the Anthropic ``system``
    field (as a dedicated first text block) but this walks message contents too,
    so a variant that inlines it is caught as well. Anything unparseable is
    returned untouched — a rewrite must never be the reason a request fails.
    """
    if not body or b"x-anthropic-billing-header" not in body.lower():
        return body, False
    try:
        import json as _j

        doc = _j.loads(body)
    except Exception:
        return body, False
    if not isinstance(doc, dict):
        return body, False

    changed = False
    if "system" in doc:
        doc["system"], hit = _clean_content(doc["system"])
        changed = changed or hit
    for msg in doc.get("messages") or []:
        if isinstance(msg, dict) and "content" in msg:
            msg["content"], hit = _clean_content(msg["content"])
            changed = changed or hit
    if not changed:
        return body, False
    return _j.dumps(doc).encode("utf-8"), True


def resolve_max_output(
    model: str, clamp_map: dict[str, int], *, fallback: int | None = None
) -> int | None:
    """Return the upstream output-token cap for ``model``.

    ``model`` may still carry a ``[1m]``-style suffix; it is normalized before
    lookup. On a miss, ``fallback`` is returned (``None`` disables clamping,
    e.g. for native Anthropic providers that accept any ``max_tokens``).
    """
    if not clamp_map:
        return fallback
    key = _norm_model(model)
    if key in clamp_map:
        return clamp_map[key]
    # tolerate a map keyed with the raw suffix form the client sent
    raw = (model or "").lower()
    if raw in clamp_map:
        return clamp_map[raw]
    return fallback


def clamp_max_tokens(
    body: bytes, clamp_map: dict[str, int], *, fallback: int | None = None
) -> tuple[bytes, int | None]:
    """Clamp the output-token cap in a request body to the upstream limit.

    Handles the three field names clients use: ``max_tokens`` (Anthropic
    /v1/messages and legacy chat), ``max_completion_tokens`` (OpenAI chat) and
    ``max_output_tokens`` (OpenAI Responses API). Returns ``(body, new_cap)``;
    ``new_cap`` is ``None`` when nothing was clamped. This prevents upstream
    ``400 ... exceeds maximum`` errors from providers whose output cap (e.g.
    CommandCode's 393216) is below what the client asks for.
    """
    if not body or not clamp_map:
        return body, None
    try:
        import json as _j
        _b = _j.loads(body)
    except Exception:
        return body, None
    if not isinstance(_b, dict):
        return body, None
    cap = resolve_max_output(_b.get("model", ""), clamp_map, fallback=fallback)
    if cap is None:
        return body, None
    changed: int | None = None
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        cur = _b.get(key)
        if isinstance(cur, int) and not isinstance(cur, bool) and cur > cap:
            _b[key] = cap
            changed = cap
    if changed is None:
        return body, None
    return _j.dumps(_b).encode("utf-8"), changed

