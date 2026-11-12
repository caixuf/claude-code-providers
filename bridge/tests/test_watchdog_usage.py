"""End-to-end: a streamed turn through the real watchdog lands in the ledger.

The unit tests prove the parser. This proves the wiring — that the tally is
attached to the chunk walk that exists for cutting, that it survives the cut at
`message_stop`, and that the numbers reach disk rather than staying in memory.
"""

import asyncio
import json
import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))

import httpx  # noqa: E402
import pytest  # noqa: E402

# starlette only comes with the proxy's own venv. Where it is missing this
# file is skipped rather than breaking the whole collection — a test that
# cannot run is not a reason for the suite to fail.
pytest.importorskip("starlette.testclient", reason="starlette is not installed here")
from starlette.testclient import TestClient  # noqa: E402

import sse_watchdog  # noqa: E402
from usage_ledger import read_ledger  # noqa: E402

START = (
    'event: message_start\n'
    'data: {"type":"message_start","message":{"model":"DeepSeek-V4.1-Flash",'
    '"usage":{"input_tokens":88,"output_tokens":1,"cache_read_input_tokens":4000}}}\n\n'
)
TEXT = (
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}\n\n'
)
DELTA = 'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":250}}\n\n'
STOP = 'event: message_stop\ndata: {"type":"message_stop"}\n\n'
# A frame after message_stop: it must be neither forwarded nor counted twice.
TRAILER = 'event: ping\ndata: {"type":"ping"}\n\n'


class _FakeStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for c in self.chunks:
            yield c

    async def aclose(self):
        pass


def _app(tmp_path, body_chunks, content_type="text/event-stream", status=200,
         delay=0.0, extra_headers=None):
    app = sse_watchdog.build_app(
        "http://127.0.0.1:4001",
        0.0,
        90.0,
        usage_path=str(tmp_path / "usage.jsonl"),
    )

    client = app.state.client

    async def fake_send(req, stream=False):
        if delay:
            await asyncio.sleep(delay)
        headers = {"content-type": content_type}
        headers.update(extra_headers or {})
        return httpx.Response(
            status,
            headers=headers,
            stream=_FakeStream(body_chunks),
            request=req,
        )

    client.send = fake_send
    return app, tmp_path / "usage.jsonl"


def test_streamed_turn_records_queue_and_end_to_end_time(tmp_path):
    """`duration_ms` alone hides the provider wait.

    It starts when the upstream response headers arrive, so a slow first byte
    looked like a fast call. The turn must also carry queue_ms (entry ->
    headers), ttft_ms (entry -> first byte), and total_ms (entry -> close),
    plus LiteLLM's own upstream/overhead numbers when the proxy supplies them.
    """
    payload = (START + TEXT + DELTA + STOP).encode()
    app, ledger_path = _app(
        tmp_path, [payload], delay=0.05,
        extra_headers={
            "x-litellm-response-duration-ms": "123.4",
            "x-litellm-overhead-duration-ms": "5.0",
        },
    )
    tc = TestClient(app)
    tc.post("/v1/messages", json={"model": "deepseek-v4.1-flash", "stream": True})

    rec = read_ledger(ledger_path)[0]
    assert rec["queue_ms"] >= 45, rec
    assert rec["total_ms"] >= rec["queue_ms"], rec
    assert rec["ttft_ms"] >= rec["queue_ms"], rec
    assert rec["upstream_ms"] == 123
    assert rec["overhead_ms"] == 5


def test_non_stream_turn_records_the_wait_before_the_body(tmp_path):
    """The old clock started after `client.send`, so a 2s call could record 0ms."""
    body = json.dumps(
        {"model": "MiniMax-M3.1-Flash", "usage": {"input_tokens": 40, "output_tokens": 60}}
    ).encode()
    app, ledger_path = _app(tmp_path, [body], content_type="application/json", delay=0.05)
    tc = TestClient(app)
    tc.post("/v1/messages", json={"model": "minimax-MiniMax-M3.1-Flash"})

    rec = read_ledger(ledger_path)[0]
    assert rec["queue_ms"] >= 45, rec
    assert rec["total_ms"] >= 45, rec
    assert rec["duration_ms"] >= 0, rec


def test_streamed_turn_is_recorded_once(tmp_path):
    payload = (START + TEXT + DELTA + STOP + TRAILER).encode()
    app, ledger_path = _app(tmp_path, [payload])
    tc = TestClient(app)
    r = tc.post("/v1/messages", json={"model": "deepseek-v4.1-flash", "stream": True})
    assert r.status_code == 200
    assert "message_stop" in r.text
    # the trailer never reaches the client
    assert r.text.count("ping") == 0

    recs = read_ledger(ledger_path)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["model"] == "DeepSeek-V4.1-Flash"
    assert rec["input_tokens"] == 88
    assert rec["cache_read_input_tokens"] == 4000
    assert rec["output_tokens"] == 250
    assert rec["streamed"] is True
    assert rec["status"] == 200
    # billed input counts the cache; a total without it reads as nearly free
    assert rec["total"] == 88 + 4000 + 250


def test_stream_split_across_chunks(tmp_path):
    payload = (START + DELTA + STOP).encode()
    chunks = [payload[i : i + 7] for i in range(0, len(payload), 7)]
    app, ledger_path = _app(tmp_path, chunks)
    tc = TestClient(app)
    tc.post("/v1/messages", json={"model": "deepseek-v4.1-flash", "stream": True})
    recs = read_ledger(ledger_path)
    assert len(recs) == 1
    assert recs[0]["output_tokens"] == 250
    assert recs[0]["input_tokens"] == 88


def test_non_stream_turn_is_recorded(tmp_path):
    body = json.dumps(
        {"model": "MiniMax-M3.1-Flash", "usage": {"input_tokens": 40, "output_tokens": 60}}
    ).encode()
    app, ledger_path = _app(tmp_path, [body], content_type="application/json")
    tc = TestClient(app)
    tc.post("/v1/messages", json={"model": "minimax-MiniMax-M3.1-Flash"})
    recs = read_ledger(ledger_path)
    assert len(recs) == 1
    assert recs[0]["streamed"] is False
    assert recs[0]["total"] == 100


def test_endpoint_without_usage_is_not_logged(tmp_path):
    """`/health`, `/v1/models` share the port and must not appear as a 0-token turn."""
    app, ledger_path = _app(tmp_path, [b'{"data":[{"id":"x"}]}'], content_type="application/json")
    tc = TestClient(app)
    tc.get("/v1/models")
    assert read_ledger(ledger_path) == []


def test_failed_request_is_not_logged(tmp_path):
    app, ledger_path = _app(
        tmp_path, [b'{"error":"nope"}'], content_type="application/json", status=500
    )
    tc = TestClient(app)
    tc.post("/v1/messages", json={"model": "x"})
    assert read_ledger(ledger_path) == []


def test_usage_can_be_switched_off(tmp_path):
    app = sse_watchdog.build_app("http://127.0.0.1:4001", 0.0, 90.0, usage_path=None)
    assert app.state.ledger is None


# ── media downgrade, end to end ──────────────────────────────────────────
# The unit tests prove the rewrite. This proves the wiring: a request that
# carries an image to a text-only model reaches the upstream without it, so
# the upstream cannot answer 400 and kill the turn.


def _capture_app(tmp_path, request_rules, response_chunks=None, content_type="application/json"):
    """A proxy whose upstream records the body it was handed."""
    app = sse_watchdog.build_app(
        "http://127.0.0.1:4001",
        0.0,
        90.0,
        usage_path=None,
        request_rules=request_rules,
    )
    client = app.state.client
    seen: list[bytes] = []

    async def fake_send(req, stream=False):
        seen.append(req.content)
        return httpx.Response(
            200,
            headers={"content-type": content_type},
            stream=_FakeStream(response_chunks or [b'{"ok":true}']),
            request=req,
        )

    client.send = fake_send
    return app, seen


def test_image_is_removed_for_a_text_only_model(tmp_path):
    """The upstream must never see an image_url for a model that cannot read it."""
    app, seen = _capture_app(
        tmp_path, {"media_downgrade": {"qianfan-deepseek-v4-flash"}}
    )
    tc = TestClient(app)
    tc.post(
        "/v1/chat/completions",
        json={
            "model": "qianfan-deepseek-v4-flash",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this?"},
                        {"type": "image_url",
                         "image_url": {"url": "data:image/png;base64,AAAA"}},
                    ],
                }
            ],
        },
    )
    assert seen, "upstream was never called"
    body = seen[0].decode()
    assert "AAAA" not in body
    assert "cannot read images" in body
    assert "what is this?" in body


def test_a_vision_model_keeps_its_image(tmp_path):
    """A model the registry calls vision-capable must keep its pixels."""
    app, seen = _capture_app(
        tmp_path, {"media_downgrade": {"qianfan-deepseek-v4-flash"}}
    )
    tc = TestClient(app)
    tc.post(
        "/v1/chat/completions",
        json={
            "model": "qianfan-deepseek-v4.1-flash",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this?"},
                        {"type": "image_url",
                         "image_url": {"url": "data:image/png;base64,AAAA"}},
                    ],
                }
            ],
        },
    )
    body = seen[0].decode()
    assert "AAAA" in body
    assert "cannot read images" not in body


def test_the_1m_spelling_is_downgraded_too(tmp_path):
    """Claude Code appends `[1m]`; the rule is matched after normalisation."""
    app, seen = _capture_app(
        tmp_path, {"media_downgrade": {"qianfan-deepseek-v4-flash"}}
    )
    tc = TestClient(app)
    tc.post(
        "/v1/chat/completions",
        json={
            "model": "qianfan-deepseek-v4-flash[1m]",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "hi"},
                        {"type": "image_url",
                         "image_url": {"url": "data:image/png;base64,AAAA"}},
                    ],
                }
            ],
        },
    )
    assert "AAAA" not in seen[0].decode()
