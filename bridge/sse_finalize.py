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


def _norm_model(name: str) -> str:
    """Normalize a model name for clamp-map lookup: strip any [..] tier suffix, lower-case."""
    if not isinstance(name, str):
        return ""
    base = name.split("[", 1)[0].strip()
    return base.lower()


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
    """Clamp ``max_tokens`` in an Anthropic /v1/messages body to the upstream cap.

    Returns ``(body, new_max_tokens)``. ``new_max_tokens`` is ``None`` when the
    body was left untouched (non-JSON, no ``max_tokens``, or already <= cap).
    This prevents ``400 Invalid max_tokens value`` from providers whose output
    cap (e.g. CommandCode's 393216) is below the context window Claude Code
    sends (1,000,000).
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
    cur = _b.get("max_tokens")
    if not isinstance(cur, int) or isinstance(cur, bool):
        return body, None
    cap = resolve_max_output(_b.get("model", ""), clamp_map, fallback=fallback)
    if cap is None or cur <= cap:
        return body, None
    _b["max_tokens"] = cap
    return _j.dumps(_b).encode("utf-8"), cap

