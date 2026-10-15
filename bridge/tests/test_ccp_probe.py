import asyncio
import json
import pathlib
import sys
import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bridge"))

from ccp_probe import (
    DEFAULT_BRIDGE_URL,
    DEFAULT_MASTER_KEY,
    _installed_endpoint,
    load_registry as _load_registry,
    strip_ansi,
    pad_right,
    pad_left,
    get_configured_models,
    sort_results,
    probe_single_model,
    print_table,
)


def test_ansi_padding():
    plain = "test"
    colored = "\033[32mtest\033[0m"

    assert strip_ansi(plain) == "test"
    assert strip_ansi(colored) == "test"

    padded_plain_r = pad_right(plain, 10)
    padded_colored_r = pad_right(colored, 10)
    assert len(strip_ansi(padded_plain_r)) == 10
    assert len(strip_ansi(padded_colored_r)) == 10

    padded_plain_l = pad_left(plain, 8)
    padded_colored_l = pad_left(colored, 8)
    assert len(strip_ansi(padded_plain_l)) == 8
    assert len(strip_ansi(padded_colored_l)) == 8


def test_get_configured_models_default():
    candidates = get_configured_models()
    assert len(candidates) > 0

    providers = {c["provider"] for c in candidates}
    assert "group" in providers
    assert "trae" in providers

    for c in candidates:
        assert "wire_name" in c
        assert "display_name" in c
        assert "ctx" in c
        assert "vision" in c


def test_get_configured_models_target_provider():
    trae_models = get_configured_models(target="trae")
    assert len(trae_models) > 0
    for c in trae_models:
        assert c["provider"] == "trae"
        assert not c["display_name"].startswith("trae-")


def test_get_configured_models_specific_target():
    qwen = get_configured_models(target="qwen3.8-max")
    assert len(qwen) >= 1
    assert any("qwen" in c["wire_name"].lower() for c in qwen)


def test_get_configured_models_all():
    primary = get_configured_models()
    all_models = get_configured_models(all_models=True)
    assert len(all_models) >= len(primary)


def test_sort_results_modes():
    results = [
        {"display_name": "m1", "available": True, "rounds_passed": 2, "rounds_total": 2, "tps": 50.0, "ttft_ms": 1000.0, "total_ms": 1500.0},
        {"display_name": "m2", "available": True, "rounds_passed": 2, "rounds_total": 2, "tps": 120.0, "ttft_ms": 500.0, "total_ms": 800.0},
        {"display_name": "m3", "available": False, "rounds_passed": 0, "rounds_total": 2, "tps": 0.0, "ttft_ms": 0.0, "total_ms": 0.0},
        {"display_name": "m4", "available": True, "rounds_passed": 1, "rounds_total": 2, "tps": 200.0, "ttft_ms": 300.0, "total_ms": 500.0},
    ]

    # Sort by speed: 100% stable models first (m2, m1), then flaky (m4), then dead (m3)
    by_speed = sort_results(results, sort_key="speed")
    assert [r["display_name"] for r in by_speed] == ["m2", "m1", "m4", "m3"]

    # Sort by TTFT: 100% stable models first by lowest TTFT
    by_ttft = sort_results(results, sort_key="ttft")
    assert [r["display_name"] for r in by_ttft] == ["m2", "m1", "m4", "m3"]

    # Sort by total turnaround
    by_total = sort_results(results, sort_key="total")
    assert [r["display_name"] for r in by_total] == ["m2", "m1", "m4", "m3"]

    # Default sort (balanced score): m2 (high TPS & low TTFT) beats m1, and both beat flaky m4
    by_default = sort_results(results, sort_key="default")
    assert [r["display_name"] for r in by_default] == ["m2", "m1", "m4", "m3"]


@pytest.mark.anyio
async def test_probe_single_model_success_1_round():
    spec = {
        "wire_name": "test-model",
        "display_name": "model",
        "provider": "testprov",
        "ctx": "1M",
        "vision": True,
    }

    # Anthropic Messages SSE — the transport the probe actually uses. The old
    # fixtures were OpenAI `choices`/`delta`, which this path never sees.
    sse_lines = [
        b'data: {"type": "message_start", "message": {"usage": {"input_tokens": 9}}}\n\n',
        b'data: {"type": "content_block_start", "index": 0}\n\n',
        b'data: {"type": "content_block_delta", "index": 0, "delta": {"text": "Hello"}}\n\n',
        b'data: {"type": "content_block_delta", "index": 0, "delta": {"text": " world"}}\n\n',
        b'data: {"type": "message_delta", "usage": {"output_tokens": 10, "cache_read_input_tokens": 5}}\n\n',
        b'data: {"type": "message_stop"}\n\n',
    ]

    async def mock_aiter_lines():
        for line in sse_lines:
            yield line.decode("utf-8").strip()

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.aiter_lines = mock_aiter_lines

    mock_stream_ctx = AsyncMock()
    mock_stream_ctx.__aenter__.return_value = mock_resp
    mock_stream_ctx.__aexit__.return_value = None

    client = MagicMock()
    client.stream = MagicMock(return_value=mock_stream_ctx)

    res = await probe_single_model(client, spec, timeout_s=5.0, rounds=1)

    assert res["available"] is True
    assert res["status"] == "OK"
    assert res["rounds_passed"] == 1
    assert res["rounds_total"] == 1
    assert res["tokens"] == 10
    assert res["cached_tokens"] == 5
    assert res["ttft_ms"] >= 0
    assert res["tps"] > 0


@pytest.mark.anyio
async def test_probe_single_model_multi_round():
    spec = {
        "wire_name": "test-model",
        "display_name": "model",
        "provider": "testprov",
        "ctx": "1M",
        "vision": True,
    }

    sse_lines = [
        b'data: {"type": "message_start"}\n\n',
        b'data: {"type": "content_block_delta", "index": 0, "delta": {"text": "Test"}}\n\n',
        b'data: {"type": "content_block_delta", "index": 0, "delta": {"text": " response"}}\n\n',
        b'data: {"type": "message_delta", "usage": {"output_tokens": 20}}\n\n',
    ]

    async def mock_aiter_lines():
        for line in sse_lines:
            yield line.decode("utf-8").strip()

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.aiter_lines = mock_aiter_lines

    mock_stream_ctx = AsyncMock()
    mock_stream_ctx.__aenter__.return_value = mock_resp
    mock_stream_ctx.__aexit__.return_value = None

    client = MagicMock()
    client.stream = MagicMock(return_value=mock_stream_ctx)

    res = await probe_single_model(client, spec, timeout_s=5.0, rounds=2)

    assert res["available"] is True
    assert res["status"] == "2/2"
    assert res["rounds_passed"] == 2
    assert res["rounds_total"] == 2
    assert res["tokens"] == 20
    assert res["tps"] > 0


@pytest.mark.anyio
async def test_probe_single_model_http_error():
    spec = {
        "wire_name": "bad-model",
        "display_name": "bad",
        "provider": "testprov",
    }

    mock_resp = AsyncMock()
    mock_resp.status_code = 401
    mock_resp.aread.return_value = json.dumps({"error": {"message": "Invalid API Key"}}).encode("utf-8")

    mock_stream_ctx = AsyncMock()
    mock_stream_ctx.__aenter__.return_value = mock_resp
    mock_stream_ctx.__aexit__.return_value = None

    client = MagicMock()
    client.stream = MagicMock(return_value=mock_stream_ctx)

    res = await probe_single_model(client, spec, timeout_s=5.0, rounds=2)

    assert res["available"] is False
    assert res["status"] == "HTTP 401"
    assert "Invalid API Key" in res["error_msg"]


@pytest.mark.anyio
async def test_probe_single_model_connect_error():
    spec = {"wire_name": "down-model", "display_name": "down", "provider": "testprov"}

    mock_stream_ctx = AsyncMock()
    mock_stream_ctx.__aenter__.side_effect = httpx.ConnectError("Connection refused")

    client = MagicMock()
    client.stream = MagicMock(return_value=mock_stream_ctx)

    res = await probe_single_model(client, spec, timeout_s=5.0, rounds=2)

    assert res["available"] is False
    assert res["status"] == "Unreachable"


@pytest.mark.anyio
async def test_probe_single_model_timeout():
    spec = {"wire_name": "slow-model", "display_name": "slow", "provider": "testprov"}

    mock_stream_ctx = AsyncMock()
    mock_stream_ctx.__aenter__.side_effect = httpx.TimeoutException("Timed out")

    client = MagicMock()
    client.stream = MagicMock(return_value=mock_stream_ctx)

    res = await probe_single_model(client, spec, timeout_s=5.0, rounds=2)

    assert res["available"] is False
    assert res["status"] == "Timeout"


def test_print_table(capsys):
    results = [
        {
            "provider": "trae",
            "display_name": "fast-model",
            "available": True,
            "status": "2/2",
            "rounds_passed": 2,
            "rounds_total": 2,
            "ttft_ms": 250.0,
            "tps": 100.0,
            "total_ms": 400.0,
            "ctx": "1M",
            "vision": True,
        },
        {
            "provider": "trae",
            "display_name": "dead-model",
            "available": False,
            "status": "HTTP 400",
            "rounds_passed": 0,
            "rounds_total": 2,
            "error_msg": "Bad Request",
            "ttft_ms": 0.0,
            "tps": 0.0,
            "total_ms": 0.0,
            "ctx": "128k",
            "vision": False,
        },
    ]

    best = print_table(results, sort_key="speed", rounds=2, tokens=60)
    captured = capsys.readouterr()

    assert "fast-model" in captured.out
    assert "dead-model" in captured.out
    assert "Recommended: fast-model" in captured.out
    assert "Summary: 1/2 models responsive" in captured.out
    assert best["display_name"] == "fast-model"


# ── candidates must cover what a provider actually offers ────────────────────
# `ccp ping deepseek` used to report "No models found": candidates came only from
# the alias table, and deepseek/minimax are native with no aliases at all. These
# pin the counts the folder listing shows, so the two cannot drift apart again.


def test_every_provider_has_candidates():
    """A provider that cannot be probed is the bug this guards."""
    for name in ("cline", "deepseek", "minimax", "opendesign", "qianfan", "trae"):
        found = get_configured_models(target=name)
        assert found, f"{name} yielded no probe candidates"


def test_native_providers_are_probed_not_skipped():
    """deepseek and minimax have zero aliases; they are addressed by upstream id."""
    deepseek = get_configured_models(target="deepseek")
    assert [c["wire_name"] for c in deepseek] == ["deepseek-flash", "deepseek-v4-pro"]
    assert all(c["native"] for c in deepseek)

    minimax = get_configured_models(target="minimax")
    assert len(minimax) == 8
    # A native endpoint wants the vendor's own id — no alias prefix, no [1m].
    assert "MiniMax-M3" in [c["wire_name"] for c in minimax]


def test_bridged_providers_use_the_alias_litellm_accepts():
    """Bridged goes through LiteLLM, which only knows the alias — `[1m]` included."""
    q = get_configured_models(target="qianfan")
    names = [c["wire_name"] for c in q]
    assert "qianfan-deepseek-v4.1-flash[1m]" in names
    assert not any(c["native"] for c in q)


def test_each_candidate_carries_its_own_endpoint_and_credential():
    """One base_url cannot serve both shapes: native providers bypass the bridge."""
    spec = get_configured_models(target="deepseek")[0]
    assert spec["endpoint"].startswith("https://api.deepseek.com/")
    assert spec["token"] and spec["token"] != DEFAULT_MASTER_KEY

    # Trae is archived (its profile sits in ~/.claude/providers_disabled), so a
    # local install legitimately has no endpoint for it and the candidate falls
    # back to the bridge. Assert the adapter's contract only when the profile is
    # actually installed — otherwise this test fails for a machine that is
    # configured exactly as intended.
    trae = get_configured_models(target="trae")
    if trae and _installed_endpoint("trae")[0]:
        assert trae[0]["endpoint"].startswith("http://127.0.0.1:4100")

    spec = get_configured_models(target="qianfan")[0]
    assert spec["endpoint"] == DEFAULT_BRIDGE_URL
    assert spec["token"] == DEFAULT_MASTER_KEY


def test_candidate_counts_match_the_folder_listing():
    """`ccp`, `ccp models` and `ccp ping` must not disagree about a provider."""
    import ccp_pick

    p_doc, m_doc = _load_registry_docs()
    for prov in ccp_pick._providers(p_doc, m_doc):
        assert len(get_configured_models(target=prov.name)) == len(prov.models), (
            f"{prov.name}: ping sees a different set than the folder"
        )


def test_a_native_model_absent_from_litellm_is_still_probeable():
    """`MiniMax-M2` has no LiteLLM entry, so probing it via :4000 would 400.

    Its candidate must still name it and point at the vendor endpoint.
    """
    found = get_configured_models(target="MiniMax-M2")
    assert len(found) == 1
    assert found[0]["wire_name"] == "MiniMax-M2"
    assert found[0]["endpoint"].startswith("https://api.minimaxi.com/")


def test_groups_are_probed_through_the_bridge():
    groups = get_configured_models(target="group")
    assert groups, "routing groups disappeared from the probe"
    for g in groups:
        assert g["provider"] == "group"
        assert g["endpoint"] == DEFAULT_BRIDGE_URL


def _load_registry_docs():
    return _load_registry()
