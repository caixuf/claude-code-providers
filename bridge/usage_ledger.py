"""Read a model's usage out of an Anthropic response and total it per day.

Two shapes arrive on the bridge, and both carry the same four numbers:

* **streaming** — `message_start` reports the input side (including
  `cache_read_input_tokens` and `cache_creation_input_tokens`), the closing
  `message_delta` reports the running output count. Neither event is the whole
  picture, so they are merged rather than taken from whichever came last.
* **non-streaming** — one `usage` object with all four.

Everything here is a pure function of bytes; writing to disk lives in
`UsageLedger`. That split is what lets the parsing be tested against captured
wire traffic instead of a live provider.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# The four counters Anthropic defines. Anything else a gateway invents
# (`server_tool_use`, `service_tier`, …) is carried in `extra` so nothing is
# silently dropped, but it never enters a total.
FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    extra: dict = field(default_factory=dict)

    def merge(self, other: "Usage") -> "Usage":
        """Take each counter's newest value, which is what `message_delta` means.

        Anthropic's `message_delta.usage` is cumulative, not incremental: the
        last one wins. Merging by max would double-count a model that emits a
        smaller final figure after a retry, and merging by sum counts every
        delta twice. So each non-zero value overwrites.
        """
        for f in FIELDS:
            v = getattr(other, f)
            if v:
                setattr(self, f, v)
        if other.extra:
            self.extra.update(other.extra)
        return self

    def is_empty(self) -> bool:
        return not any(getattr(self, f) for f in FIELDS)

    @property
    def billed_input(self) -> int:
        """Input the plan charges for: fresh + cache-read + cache-write.

        Anthropic reports the three separately because they are priced
        differently, but a token total that leaves the cache out reads as a
        nearly-free session, which is the opposite of the truth on a long one.
        """
        return (
            self.input_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
        )

    @property
    def total(self) -> int:
        return self.billed_input + self.output_tokens

    def as_dict(self) -> dict:
        out = {f: getattr(self, f) for f in FIELDS}
        out["total"] = self.total
        if self.extra:
            out["extra"] = self.extra
        return out


def _as_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def usage_from_obj(obj) -> Usage | None:
    """Pull a `usage` mapping off a decoded Anthropic object, if it has one."""
    if not isinstance(obj, dict):
        return None
    u = obj.get("usage")
    if not isinstance(u, dict):
        return None
    us = Usage()
    for f in FIELDS:
        setattr(us, f, _as_int(u.get(f)))
    for k, v in u.items():
        if k not in FIELDS:
            us.extra[k] = v
    return None if us.is_empty() else us


def _sse_json_events(buf: bytes):
    """Yield the decoded JSON of every `data:` line in a complete SSE event."""
    for raw in buf.split(b"\n"):
        line = raw.strip()
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            continue
        try:
            yield json.loads(payload)
        except Exception:
            continue


class UsageTally:
    """Accumulates one request's usage from streamed SSE chunks.

    Fed incrementally by the watchdog, which is already walking the stream to
    decide where to cut it — so this costs no extra buffering. Chunks are split
    on event boundaries by the caller and handed over whole; a partial event
    left over is returned to the caller to prepend to the next one.
    """

    def __init__(self) -> None:
        self.usage = Usage()
        self.model = ""
        self.saw_stream = False
        self.buf = b""

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.saw_stream = True
        data = self.buf + chunk
        # Split on blank lines only; a chunk that ends mid-event must not be
        # parsed, or a truncated `message_delta` reports a partial token count.
        while True:
            idx_lf = data.find(b"\n\n")
            idx_crlf = data.find(b"\r\n\r\n")
            if idx_lf == -1 and idx_crlf == -1:
                break
            if idx_crlf != -1 and (idx_lf == -1 or idx_crlf < idx_lf):
                event, data = data[:idx_crlf], data[idx_crlf + 4 :]
            else:
                event, data = data[:idx_lf], data[idx_lf + 2 :]
            self._absorb(event)
        self.buf = data

    def _absorb(self, event: bytes) -> None:
        for obj in _sse_json_events(event):
            if not isinstance(obj, dict):
                continue
            # message_start carries the model and the input side; message_delta
            # carries the output side. `message_start` nests its payload one
            # level down under "message", so the top-level lookup finds nothing
            # and the input side would silently read as zero.
            inner = obj.get("message") if isinstance(obj.get("message"), dict) else None
            for src in (obj, inner):
                if not src:
                    continue
                if not self.model:
                    name = src.get("model")
                    if isinstance(name, str) and name:
                        self.model = name
                u = usage_from_obj(src)
                if u is not None:
                    self.usage.merge(u)

    def absorb_json(self, body: bytes) -> None:
        """Non-streaming reply: one JSON object with a whole `usage`."""
        if not body:
            return
        try:
            obj = json.loads(body)
        except Exception:
            return
        if not isinstance(obj, dict):
            return
        name = obj.get("model")
        if isinstance(name, str) and name:
            self.model = name
        u = usage_from_obj(obj)
        if u is not None:
            self.usage.merge(u)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class UsageLedger:
    """Append-only JSONL of finished requests, one line each.

    Appending rather than rewriting a summary is deliberate: a crash mid-write
    loses at most the last line, and `ccp usage` can re-aggregate any window
    (today, this week, one model) without the writer having anticipated it. A
    rewritten total is the shape that goes to zero when the process dies during
    the write — the failure that cost magpie its state files.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else Path.home() / ".local/state/ccp/usage.jsonl"

    def append(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # One write() of one line under O_APPEND: the kernel makes the line
            # atomic against other writers, so concurrent adapters interleave
            # lines rather than bytes.
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, (line + "\n").encode())
            finally:
                os.close(fd)
        except Exception:
            # Usage is diagnostics, never the request itself: a full disk must
            # not take down the proxy that is carrying the conversation.
            pass

    def record(
        self,
        *,
        provider: str,
        model: str,
        usage: Usage,
        status: int | None = None,
        duration_ms: int | None = None,
        streamed: bool = False,
        ttft_ms: int | None = None,
    ) -> None:
        if usage is None or usage.is_empty():
            return
        rec = {
            "ts": _now_iso(),
            "provider": provider,
            "model": model or "unknown",
            "streamed": streamed,
            **usage.as_dict(),
        }
        if status is not None:
            rec["status"] = status
        if duration_ms is not None:
            rec["duration_ms"] = duration_ms
        if ttft_ms is not None:
            rec["ttft_ms"] = ttft_ms
        self.append(rec)


def read_ledger(path: str | Path) -> list[dict]:
    """Every readable line. A corrupt line is skipped, never fatal."""
    p = Path(path)
    if not p.is_file():
        return []
    out = []
    try:
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        return out
    return out


def _day_of(rec: dict) -> str:
    ts = rec.get("ts") or ""
    return ts[:10] if len(ts) >= 10 else "unknown"


@dataclass
class Bucket:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def add(self, rec: dict) -> None:
        self.requests += 1
        for f in FIELDS:
            v = rec.get(f)
            if isinstance(v, int):
                setattr(self, f, getattr(self, f) + v)

    @property
    def billed_input(self) -> int:
        return (
            self.input_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
        )

    @property
    def total(self) -> int:
        return self.billed_input + self.output_tokens


def aggregate(records: list[dict], key: str) -> dict[str, Bucket]:
    """Group by `key` — 'day', 'model' or 'provider'. Unknown keys are kept."""
    out: dict[str, Bucket] = {}
    for rec in records:
        if key == "day":
            k = _day_of(rec)
        else:
            k = str(rec.get(key) or "unknown")
        out.setdefault(k, Bucket()).add(rec)
    return out


def sort_keys(grouped: dict, key: str) -> list[str]:
    """Days ascending (chronology reads better); models/providers by total."""
    if key == "day":
        return sorted(grouped)
    return sorted(grouped, key=lambda k: grouped[k].total, reverse=True)
