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
    while b"\n\n" in buf:
        part, buf = buf.split(b"\n\n", 1)
        events.append(part + b"\n\n")
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
