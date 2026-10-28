"""The bridge absorbs the client's credential.

LiteLLM accepts exactly one credential: the master key in the config it was
started with. A client that carries another one gets 400 "No connected db.",
and a client that carries none gets a 500 out of LiteLLM's own error handler
- both read like the bridge is down, and neither has to be able to stop a
call. So the proxy forwards the key LiteLLM accepts, whatever the client sent.
"""
from __future__ import annotations

import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))

import httpx  # noqa: E402
import pytest  # noqa: E402

# starlette only comes with the proxy's own venv. Where it is missing this
# file is skipped rather than breaking the whole collection.
pytest.importorskip("starlette.testclient", reason="starlette is not installed here")
from starlette.testclient import TestClient  # noqa: E402

import sse_watchdog  # noqa: E402

KEY = "sk-ccp-litellm-local"


class _FakeStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for c in self.chunks:
            yield c

    async def aclose(self):
        pass


def _app(tmp_path, master_key=KEY):
    """A proxy whose upstream records the headers it was handed."""
    app = sse_watchdog.build_app(
        "http://127.0.0.1:4001",
        0.0,
        90.0,
        usage_path=None,
        master_key=master_key,
    )
    seen: dict = {}

    async def fake_send(req, stream=False):
        seen["headers"] = dict(req.headers)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=_FakeStream([b'{"data":[{"id":"x"}]}']),
            request=req,
        )

    app.state.client.send = fake_send
    return app, seen


def test_a_stale_client_key_does_not_reach_the_upstream(tmp_path):
    app, seen = _app(tmp_path)
    tc = TestClient(app)
    r = tc.get("/v1/models", headers={"Authorization": "Bearer sk-cmdc-litellm-local"})
    assert r.status_code == 200
    assert seen["headers"]["authorization"] == f"Bearer {KEY}"


def test_a_client_with_no_key_gets_one(tmp_path):
    app, seen = _app(tmp_path)
    tc = TestClient(app)
    r = tc.get("/v1/models")
    assert r.status_code == 200
    assert seen["headers"]["authorization"] == f"Bearer {KEY}"


def test_the_right_key_is_forwarded_unchanged(tmp_path):
    app, seen = _app(tmp_path)
    tc = TestClient(app)
    r = tc.get("/v1/models", headers={"Authorization": f"Bearer {KEY}"})
    assert r.status_code == 200
    assert seen["headers"]["authorization"] == f"Bearer {KEY}"


def test_the_other_headers_survive_the_swap(tmp_path):
    app, seen = _app(tmp_path)
    tc = TestClient(app)
    tc.post(
        "/v1/messages",
        json={"model": "x", "messages": []},
        headers={"Authorization": "Bearer stale", "anthropic-version": "2023-06-01"},
    )
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
