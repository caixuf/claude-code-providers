"""The machine-consistency check.

Three things drift silently, and each fails late: a profile left installed for
a retired provider, the active profile naming a model that is gone, and the
Hermes bot left on a retired model — which only fails when somebody messages
it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))

import ccp_doctor  # noqa: E402


def test_hermes_models_are_read_without_a_yaml_parser():
    """A Hermes config is hand-written and heavily commented.

    A parser would need the whole document to be valid to answer one question,
    and a config that is halfway edited must still be reportable.
    """
    cfg = """
model:
  # Default model to use
  default: "qianfan-deepseek-v4.1-flash"
  provider: "custom:ccp"

providers:
  ccp:
    default_model: qianfan-deepseek-v4.1-flash   # the one the bot uses
"""
    path = Path("/tmp/hermes-test-config.yaml")
    path.write_text(cfg)
    models = ccp_doctor.hermes_models(path)
    assert "qianfan-deepseek-v4.1-flash" in models
    assert "custom:ccp" not in models, "a provider name is not a model"
    assert models == ["qianfan-deepseek-v4.1-flash"]


def test_a_commented_out_model_is_not_a_model():
    path = Path("/tmp/hermes-test-config2.yaml")
    path.write_text("model:\n  # default: \"gone-model\"\n  default: \"live-model\"\n")
    assert ccp_doctor.hermes_models(path) == ["live-model"]


def test_the_doctor_finds_a_dead_profile_and_a_dead_bot(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    (repo / "providers").mkdir(parents=True)
    (repo / "providers" / "qianfan.json.example").write_text(json.dumps({
        "env": {"ANTHROPIC_MODEL": "qianfan-deepseek-v4.1-flash"},
    }))

    providers = tmp_path / "providers"
    providers.mkdir()
    (providers / "workbuddy.json").write_text(json.dumps({
        "env": {"ANTHROPIC_MODEL": "workbuddy-deepseek-v4.1-flash"},
    }))

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "env": {"ANTHROPIC_MODEL": "workbuddy-deepseek-v4.1-flash"},
    }))

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        'model:\n  default: "workbuddy-deepseek-v4.1-flash"\n'
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    rc = ccp_doctor.main([
        "--providers-dir", str(providers),
        "--settings", str(settings),
        "--repo", str(repo),
        "--bridge", "",
    ])
    out = capsys.readouterr().out
    assert rc == 1
    assert "workbuddy.json" in out
    assert "the active profile asks for" in out
    assert "the Hermes bot at" in out


def test_a_clean_machine_says_so(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    (repo / "providers").mkdir(parents=True)
    (repo / "providers" / "qianfan.json.example").write_text(json.dumps({
        "env": {"ANTHROPIC_MODEL": "qianfan-deepseek-v4.1-flash"},
    }))
    providers = tmp_path / "providers"
    providers.mkdir()
    (providers / "qianfan.json").write_text(json.dumps({
        "env": {"ANTHROPIC_MODEL": "qianfan-deepseek-v4.1-flash"},
    }))
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "env": {"ANTHROPIC_MODEL": "qianfan-deepseek-v4.1-flash"},
    }))
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text('model:\n  default: "qianfan-deepseek-v4.1-flash"\n')
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    rc = ccp_doctor.main([
        "--providers-dir", str(providers),
        "--settings", str(settings),
        "--repo", str(repo),
        "--bridge", "",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "everything installed still points at a model the bridge serves" in out


def test_a_stale_bridge_credential_is_reported(tmp_path, monkeypatch, capsys):
    """A config that points at the bridge carries the bridge's key.

    The credential was renamed once already, and the copy that survived in the
    live config is what made every request fail with a 400 that named neither
    the key nor the bridge. The doctor has to say so - even though the bridge
    now replaces the credential and the call works anyway.
    """
    monkeypatch.setenv("CCP_MASTER_KEY", "sk-the-bridge-key")
    repo = tmp_path / "repo"
    (repo / "providers").mkdir(parents=True)
    (repo / "providers" / "qianfan.json.example").write_text(json.dumps({
        "env": {"ANTHROPIC_MODEL": "qianfan-deepseek-v4.1-flash"},
    }))
    providers = tmp_path / "providers"
    providers.mkdir()
    (providers / "qianfan.json").write_text(json.dumps({
        "env": {"ANTHROPIC_MODEL": "qianfan-deepseek-v4.1-flash"},
    }))
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "env": {
            "ANTHROPIC_MODEL": "qianfan-deepseek-v4.1-flash",
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
            "ANTHROPIC_AUTH_TOKEN": "sk-cmdc-litellm-local",
        },
    }))
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        'model:\n  default: "qianfan-deepseek-v4.1-flash"\n'
        "providers:\n  ccp:\n"
        '    base_url: "http://127.0.0.1:4000/v1"\n'
        '    api_key: "sk-cmdc-litellm-local"\n'
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    rc = ccp_doctor.main([
        "--providers-dir", str(providers),
        "--settings", str(settings),
        "--repo", str(repo),
        "--bridge", "",
    ])
    out = capsys.readouterr().out
    # the bridge replaces the credential, so nothing is broken - but the
    # configs still say something that is not true
    assert rc == 0
    assert "not the bridge's key" in out
    assert out.count("not the bridge's key") == 2  # the active profile and the bot


def test_a_native_profile_is_not_asked_for_the_bridge_key(tmp_path, monkeypatch, capsys):
    """A vendor credential is the vendor's own; the doctor must not call it stale."""
    monkeypatch.setenv("CCP_MASTER_KEY", "sk-the-bridge-key")
    repo = tmp_path / "repo"
    (repo / "providers").mkdir(parents=True)
    (repo / "providers" / "qianfan.json.example").write_text(json.dumps({
        "env": {"ANTHROPIC_MODEL": "qianfan-deepseek-v4.1-flash"},
    }))
    providers = tmp_path / "providers"
    providers.mkdir()
    (providers / "qianfan.json").write_text(json.dumps({
        "env": {"ANTHROPIC_MODEL": "qianfan-deepseek-v4.1-flash"},
    }))
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "env": {
            "ANTHROPIC_MODEL": "qianfan-deepseek-v4.1-flash",
            "ANTHROPIC_BASE_URL": "https://api.minimax.cn/anthropic",
            "ANTHROPIC_AUTH_TOKEN": "sk-the-vendors-own-key",
        },
    }))
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text('model:\n  default: "qianfan-deepseek-v4.1-flash"\n')
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    rc = ccp_doctor.main([
        "--providers-dir", str(providers),
        "--settings", str(settings),
        "--repo", str(repo),
        "--bridge", "",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "not the bridge's key" not in out
