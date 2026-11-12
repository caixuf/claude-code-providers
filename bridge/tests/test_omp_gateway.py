"""The omp adapter turns omp's gateway catalogue into a Claude Code profile.

omp owns the logins and refreshes; ccp must only carry the gateway token and
pick model ids the gateway actually serves. These tests pin that boundary: a
missing family falls back to the primary model, the context window is the
smallest one across the chosen tiers, and a dynamic profile is never audited
against the ccp bridge (which does not serve omp's model names).
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bridge"))

import omp_gateway as og  # noqa: E402
import profile_audit  # noqa: E402


def _models():
    return [
        {"id": "google-antigravity/gemini-2.5-flash", "context_length": 1_048_576,
         "max_output_tokens": 65535, "input_modalities": ["text", "image"]},
        {"id": "anthropic/claude-opus-4-6", "context_length": 250_000,
         "max_output_tokens": 64000, "input_modalities": ["text", "image"]},
        {"id": "anthropic/claude-sonnet-4-6", "context_length": 250_000,
         "max_output_tokens": 64000, "input_modalities": ["text", "image"]},
        {"id": "anthropic/claude-haiku-4-5", "context_length": 200_000,
         "max_output_tokens": 32000, "input_modalities": ["text", "image"]},
        {"id": "opencode-go/deepseek-v4.1-flash", "context_length": 1_000_000,
         "max_output_tokens": 384000, "input_modalities": ["text", "image"]},
        {"id": "opencode-go/kimi-k3", "context_length": 1_050_000,
         "max_output_tokens": 131072, "input_modalities": ["text", "image"]},
        {"id": "opencode-zen/beam-free", "context_length": 1_048_576,
         "max_output_tokens": 131072, "input_modalities": ["text"]},
    ]


def test_opencode_go_and_zen_are_one_provider_layer():
    """`ccp omp opencode` must be a sibling of `ccp opencode`.

    omp exports the same vendor under `opencode-go/` and `opencode-zen/`; the
    ccp provider layer is the vendor, not the login plan.
    """
    assert og.upstream_of("opencode-go/deepseek-v4.1-flash") == "opencode"
    assert og.upstream_of("opencode-zen/beam-free") == "opencode"
    assert og.upstream_of("google-antigravity/gemini-2.5-flash") == "google-antigravity"
    names = set(og.upstreams(_models()))
    assert names == {"opencode", "google-antigravity", "anthropic"}
    assert len(og.models_for_upstream(_models(), "opencode")) == 3


def test_profile_can_be_scoped_to_one_upstream(monkeypatch):
    monkeypatch.setenv("OMP_AUTH_GATEWAY_TOKEN", "gateway-token")
    profile = og.profile_for(_models(), upstream="opencode")
    env = profile["env"]
    assert env["ANTHROPIC_MODEL"].startswith("opencode-")
    assert "deepseek-v4.1-flash" in env["ANTHROPIC_MODEL"]
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"].startswith("opencode-")
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"].startswith("opencode-")
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "1000000"


def test_unknown_upstream_is_rejected_with_the_known_list(monkeypatch):
    monkeypatch.setenv("OMP_AUTH_GATEWAY_TOKEN", "gateway-token")
    with pytest.raises(SystemExit) as exc:
        og.profile_for(_models(), upstream="no-such-upstream")
    assert "known:" in str(exc.value)


def test_pick_models_prefers_sonnet_for_the_default_tier():
    chosen = og.pick_models(_models())
    assert chosen["default"]["id"] == "anthropic/claude-sonnet-4-6"
    assert chosen["opus"]["id"] == "anthropic/claude-opus-4-6"
    assert chosen["haiku"]["id"] == "anthropic/claude-haiku-4-5"


def test_a_missing_family_falls_back_to_the_primary_model():
    chosen = og.pick_models([_models()[0]])
    for slot in ("default", "opus", "haiku"):
        assert chosen[slot]["id"] == "google-antigravity/gemini-2.5-flash"


def test_a_requested_model_wins_the_default_slots():
    chosen = og.pick_models(_models(), "opus")
    assert chosen["default"]["id"] == "anthropic/claude-opus-4-6"
    assert chosen["sonnet"]["id"] == "anthropic/claude-opus-4-6"


def test_profile_advertises_the_smallest_window_in_the_tier_set(monkeypatch):
    monkeypatch.setenv("OMP_AUTH_GATEWAY_TOKEN", "gateway-token")
    profile = og.profile_for(_models())
    env = profile["env"]
    assert env["ANTHROPIC_AUTH_TOKEN"] == "gateway-token"
    assert env["ANTHROPIC_BASE_URL"] == og.gateway_url()
    assert env["ANTHROPIC_MODEL"] == "anthropic/claude-sonnet-4-6"
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "anthropic/claude-opus-4-6"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "anthropic/claude-haiku-4-5"
    # 200k is the haiku tier; the 1M Gemini and 250k Claude windows must not
    # make the profile promise more than the weakest tier can honour.
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "200000"
    for key, value in env.items():
        if key.endswith("MODEL") or key == "ANTHROPIC_MODEL":
            assert "[1m]" not in value, f"{key} carries a marker omp rejects: {value}"


def test_dynamic_profile_gets_a_sidecar_and_private_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("OMP_AUTH_GATEWAY_TOKEN", "gateway-token")
    path = tmp_path / "providers" / "omp.json"
    og.write_dynamic(path, og.profile_for(_models()))
    assert json.loads(path.read_text())["model"] == "anthropic/claude-sonnet-4-6"
    assert (path.parent / "omp.json.ccp-dynamic").is_file()
    assert (path.stat().st_mode & 0o777) == 0o600


def test_profile_audit_does_not_archive_a_dynamic_profile(tmp_path):
    providers = tmp_path / "providers"
    providers.mkdir()
    path = providers / "omp.json"
    path.write_text(json.dumps({
        "model": "anthropic/claude-sonnet-4-6",
        "env": {"ANTHROPIC_MODEL": "anthropic/claude-sonnet-4-6"},
    }))
    (providers / "omp.json.ccp-dynamic").write_text("omp\n")
    assert profile_audit.rows_for(providers, served=set()) == []


def test_unit_text_wires_the_gateway_to_the_broker():
    broker, gateway = og.unit_text("/usr/local/bin/omp")
    assert "auth-broker serve --bind=127.0.0.1:8765" in broker
    assert "auth-gateway serve --bind=127.0.0.1:4010" in gateway
    assert "OMP_AUTH_BROKER_URL=http://127.0.0.1:8765" in gateway
    assert "omp-auth-broker.service" in gateway
