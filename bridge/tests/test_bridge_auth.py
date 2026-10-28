"""The bridge's credential, and the client's.

The failure this guards against is the one that reads like the bridge is
down: LiteLLM answers a client whose credential is not the master key with
400 "No connected db.", and answers a client that carries none with a 500 out
of LiteLLM's own error handler. Both are the client's mistake, and the bridge
has to absorb them - the call is about the model, not about the key.
"""
from __future__ import annotations

import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))

import bridge_auth  # noqa: E402

KEY = "sk-ccp-litellm-local"


def test_the_right_key_is_left_alone():
    headers, note = bridge_auth.normalize_auth({"authorization": f"Bearer {KEY}"})
    assert headers["authorization"] == f"Bearer {KEY}"
    assert note == ""


def test_a_stale_key_is_replaced():
    """The key the bridge used to have: the client is not wrong, the config is."""
    headers, note = bridge_auth.normalize_auth(
        {"authorization": "Bearer sk-cmdc-litellm-local"}
    )
    assert headers["authorization"] == f"Bearer {KEY}"
    assert note == "replaced"


def test_a_mistyped_key_is_replaced():
    headers, note = bridge_auth.normalize_auth({"authorization": "Bearer sk-typo"})
    assert headers["authorization"] == f"Bearer {KEY}"
    assert note == "replaced"


def test_a_missing_key_is_added():
    """No credential at all used to be a 500 out of LiteLLM's error handler."""
    headers, note = bridge_auth.normalize_auth({"content-type": "application/json"})
    assert headers["authorization"] == f"Bearer {KEY}"
    assert headers["content-type"] == "application/json"
    assert note == "added"


def test_an_api_key_header_keeps_its_own_shape():
    headers, note = bridge_auth.normalize_auth({"x-api-key": "sk-cmdc-litellm-local"})
    assert headers["x-api-key"] == KEY
    assert note == "replaced"


def test_the_header_name_does_not_matter():
    headers, _ = bridge_auth.normalize_auth({"Authorization": "Bearer stale"})
    assert headers["authorization"] == f"Bearer {KEY}"
    # the differently-cased original is gone, not left to fight the new one
    assert "Authorization" not in headers


def test_a_bare_token_without_bearer_is_still_recognised():
    headers, note = bridge_auth.normalize_auth({"authorization": KEY})
    assert headers["authorization"] == f"Bearer {KEY}"
    assert note == ""


def test_the_master_key_comes_from_the_config_litellm_runs_on(tmp_path, monkeypatch):
    """A hand-edited master key is the one LiteLLM accepts, so it is the one
    the bridge forwards."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("general_settings:\n  master_key: sk-hand-edited\n")
    monkeypatch.delenv("CCP_MASTER_KEY", raising=False)
    key, source = bridge_auth.load_master_key(cfg)
    assert key == "sk-hand-edited"
    assert source == str(cfg)


def test_the_env_overrides_the_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("general_settings:\n  master_key: sk-hand-edited\n")
    monkeypatch.setenv("CCP_MASTER_KEY", "sk-from-env")
    assert bridge_auth.load_master_key(cfg)[0] == "sk-from-env"


def test_a_config_that_cannot_be_read_falls_back_to_the_constant(tmp_path, monkeypatch):
    monkeypatch.delenv("CCP_MASTER_KEY", raising=False)
    key, source = bridge_auth.load_master_key(tmp_path / "missing.yaml")
    assert key == bridge_auth.MASTER_KEY
    assert source == "built-in"


def test_only_the_local_bridge_counts_as_the_bridge():
    assert bridge_auth.is_bridge_url("http://127.0.0.1:4000")
    assert bridge_auth.is_bridge_url("http://127.0.0.1:4000/v1")
    assert bridge_auth.is_bridge_url("http://localhost:4001")
    assert not bridge_auth.is_bridge_url("https://api.minimax.cn/anthropic")
    assert not bridge_auth.is_bridge_url("")
    assert not bridge_auth.is_bridge_url(None)
