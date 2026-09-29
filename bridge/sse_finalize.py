"""Cut Anthropic SSE as soon as the stream is actually done.

Claude Code (`claude -p`) waits until the HTTP body ends. Some OpenAI
upstreams (CommandCode Space Bunny / MiniMax via LiteLLM) never send
`[DONE]`, so LiteLLM keeps the Anthropic SSE socket open with `ping`
frames. Native `cmdc` does not use this path and exits cleanly.

Rules:
- Close immediately after ``event: message_stop``.
- After any real event, if we only see pings (or silence) for
  ``ping_idle_s``, emit a synthetic ``message_stop`` and close.
- Before the first real event, wait up to ``first_byte_s``.
"""

from __future__ import annotations

MESSAGE_STOP_MARK = b"event: message_stop"
SYNTHETIC_STOP = (
    b"event: message_stop\n"
    b'data: {"type":"message_stop"}\n'
    b"\n"
)


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
    def __init__(self, *, ping_idle_s: float = 12.0, first_byte_s: float = 90.0) -> None:
        self.ping_idle_s = ping_idle_s
        self.first_byte_s = first_byte_s
        self.saw_real = False
        self.saw_stop = False
        self.buf = b""

    def wait_budget(self) -> float:
        return self.ping_idle_s if self.saw_real else self.first_byte_s

    def feed(self, chunk: bytes) -> tuple[bytes, bool]:
        """Return (bytes_to_forward, should_close)."""
        if not chunk:
            return (b"" if self.saw_stop else SYNTHETIC_STOP, True)
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
        if self.saw_stop:
            return b""
        return SYNTHETIC_STOP
