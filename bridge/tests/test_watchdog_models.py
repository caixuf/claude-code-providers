"""The standard /v1/models endpoint must carry the context window.

LiteLLM publishes ``max_input_tokens`` on ``/v1/model/info``, not on the
OpenAI-standard ``/v1/models``. A client that only knows the standard endpoint
(Hermes is one) therefore falls back to 256K. The bridge owns the catalog, so
it merges the admin metadata into the standard list here.
"""
import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))

import httpx  # noqa: E402
import pytest  # noqa: E402

pytest.importorskip("starlette.testclient", reason="starlette is not installed here")
from starlette.testclient import TestClient  # noqa: E402

import sse_watchdog  # noqa: E402


MODELS = {
    "object": "list",
    "data": [
        {"id": "main", "object": "model", "created": 1, "owned_by": "anthropic"},
        {"id": "flash", "object": "model", "created": 1, "owned_by": "anthropic"},
    ],
}
INFO = {
    "data": [
        {
            "model_name": "main",
            "litellm_params": {
                "model": "anthropic/deepseek-v4.1-flash",
                "model_info": {"max_input_tokens": 1048000, "max_output_tokens": 328000},
            },
        },
        {
            "model_name": "flash",
            "litellm_params": {
                "model": "anthropic/deepseek-v4.1-flash",
                "model_info": {"max_input_tokens": 1048000, "max_output_tokens": 328000},
            },
        },
    ]
}


def _app(models=MODELS, info=INFO, info_status=200):
    app = sse_watchdog.build_app("http://127.0.0.1:4001", 0.0, 90.0, usage_path=None)
    client = app.state.client
    calls = []

    async def fake_send(req, stream=False, **kwargs):
        calls.append(req.url.path)
        if req.url.path == "/v1/models":
            return httpx.Response(200, json=models, request=req)
        if req.url.path == "/v1/model/info":
            return httpx.Response(info_status, json=info, request=req)
        return httpx.Response(404, json={"error": "not found"}, request=req)

    client.send = fake_send
    return app, calls


def _by_id(response):
    return {item["id"]: item for item in response.json()["data"]}


def test_models_endpoint_carries_the_context_window():
    app, calls = _app()
    tc = TestClient(app)
    response = tc.get("/v1/models")

    assert response.status_code == 200
    models = _by_id(response)
    assert models["main"]["max_input_tokens"] == 1048000
    assert models["main"]["max_output_tokens"] == 328000
    assert models["flash"]["max_input_tokens"] == 1048000
    # The standard list is built from both upstream reads.
    assert "/v1/models" in calls and "/v1/model/info" in calls


def test_models_alias_is_enriched_too():
    app, _ = _app()
    tc = TestClient(app)
    response = tc.get("/models")

    assert response.status_code == 200
    assert _by_id(response)["main"]["max_input_tokens"] == 1048000


def test_a_group_advertises_the_smallest_deployment():
    info = {
        "data": [
            {"model_name": "main", "litellm_params": {"model_info": {"max_input_tokens": 1048000}}},
            {"model_name": "main", "litellm_params": {"model_info": {"max_input_tokens": 1441792}}},
        ]
    }
    app, _ = _app(info=info)
    tc = TestClient(app)
    response = tc.get("/v1/models")

    assert _by_id(response)["main"]["max_input_tokens"] == 1048000


def test_model_info_failure_keeps_the_standard_list_intact():
    app, _ = _app(info_status=500)
    tc = TestClient(app)
    response = tc.get("/v1/models")

    assert response.status_code == 200
    models = _by_id(response)
    assert len(models) == 2
    assert "max_input_tokens" not in models["main"]


def test_a_cached_catalog_is_not_refetched_on_every_request():
    app, calls = _app()
    tc = TestClient(app)
    tc.get("/v1/models")
    tc.get("/v1/models")

    assert calls.count("/v1/models") == 1
    assert calls.count("/v1/model/info") == 1


def test_model_info_map_accepts_top_level_model_info_and_ignores_bad_values():
    payload = {
        "data": [
            {"model_name": "main", "model_info": {"max_input_tokens": 64000}},
            {"model_name": "flash", "litellm_params": {"model_info": {"max_input_tokens": 0}}},
        ]
    }
    assert sse_watchdog._model_info_map(payload) == {"main": {"max_input_tokens": 64000}}
