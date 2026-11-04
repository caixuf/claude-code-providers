"""The machine-consistency check.

Three things drift silently, and each fails late: a profile left installed for
a retired provider, the active profile naming a model that is gone, and the
Hermes bot left on a retired model — which only fails when somebody messages
it.
"""
from __future__ import annotations

import json
import socket
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


def test_a_route_name_is_not_a_model():
    """`delegation.backends.model_map.default` names an option, not a model.

    The option's own `model:` is what the bridge serves; matching the
    `default:` key alone reported every real bot as dead for a name it never
    sends upstream. `stt.*.model` is a speech model for the same reason.
    """
    path = Path("/tmp/hermes-test-config3.yaml")
    path.write_text(
        "model:\n"
        "  default: main\n"
        "fallback_model:\n"
        "  - provider: custom:imfl\n"
        "    model: MiniMax-M3.1-Flash\n"
        "auxiliary:\n"
        "  compression:\n"
        "    model: MiniMax-M3.1-Flash\n"
        "delegation:\n"
        "  backends:\n"
        "    model_map:\n"
        "      default: deep\n"
        "      options:\n"
        "        - name: deep\n"
        "          model: main\n"
        "stt:\n"
        "  local:\n"
        "    model: base\n"
    )
    models = ccp_doctor.hermes_models(path)
    assert models == ["main", "MiniMax-M3.1-Flash"], models
    assert "deep" not in models, "a route name is not a model"
    assert "base" not in models, "a speech model is not a bridge model"


def test_only_the_bots_that_run_are_audited(tmp_path, monkeypatch, capsys):
    """A profile directory is not a bot.

    Only a gateway unit (or the profile named in `active_profile`) says that
    somebody actually asks this config for a model. Auditing the rest reports a
    test fixture — one deliberately pointed at a dead model — as a broken bot.
    """
    monkeypatch.setenv("CCP_MASTER_KEY", "sk-the-bridge-key")
    repo = tmp_path / "repo"
    (repo / "providers").mkdir(parents=True)
    (repo / "providers" / "live.json.example").write_text(json.dumps({
        "env": {"ANTHROPIC_MODEL": "live"},
    }))
    providers = tmp_path / "providers"
    providers.mkdir()
    (providers / "live.json").write_text(json.dumps({"env": {"ANTHROPIC_MODEL": "live"}}))
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"env": {"ANTHROPIC_MODEL": "live"}}))

    home = tmp_path / "home"
    profiles = home / ".hermes" / "profiles"
    (profiles / "running").mkdir(parents=True)
    (profiles / "running" / "config.yaml").write_text('model:\n  default: "live"\n')
    (profiles / "fixture").mkdir()
    (profiles / "fixture" / "config.yaml").write_text('model:\n  default: "gone"\n')
    unit = home / ".config" / "systemd" / "user"
    unit.mkdir(parents=True)
    (unit / "hermes-gateway-running.service").write_text("[Unit]\n")
    (home / ".hermes" / "active_profile").write_text("running\n")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))

    rc = ccp_doctor.main([
        "--providers-dir", str(providers),
        "--settings", str(settings),
        "--repo", str(repo),
        "--bridge", "",
        "--config-dir", str(tmp_path / "litellm"),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "running" in out
    assert "fixture" not in out


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
        "--config-dir", str(tmp_path / "litellm"),
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
        "--config-dir", str(tmp_path / "litellm"),
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
        "--config-dir", str(tmp_path / "litellm"),
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
        "--config-dir", str(tmp_path / "litellm"),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "not the bridge's key" not in out


# ---------------------------------------------------------------------------
# The machine's own state: what the install left behind, and who actually
# answered the last request (PRINCIPLES.md §1 §2 §4 §5).
# ---------------------------------------------------------------------------


def test_more_than_one_backup_is_litter(tmp_path):
    cfg = tmp_path / "litellm"
    cfg.mkdir()
    (cfg / "config.yaml").write_text("live")
    for stamp in ("20260101-000000", "20260102-000000"):
        (cfg / f"config.yaml.bak.{stamp}").write_text("old")
    (cfg / "proxy.env.bak.before-keys").write_text("SECRET=1")

    problems, warnings = ccp_doctor.backup_litter(cfg)
    assert any("proxy.env" in p for p in problems), "a secret must not be restorable"
    assert any("config.yaml has 2 backups" in w for w in warnings)


def test_one_backup_per_file_is_fine(tmp_path):
    cfg = tmp_path / "litellm"
    cfg.mkdir()
    (cfg / "config.yaml.bak.20260101-000000").write_text("old")
    assert ccp_doctor.backup_litter(cfg) == ([], [])


def test_two_names_for_the_same_secret_are_reported():
    """2026-11-04: MINIMAX_PLAN_API_KEY was overwritten with MINIMAX_API_KEY's
    value, the primary group answered 402, and LiteLLM fell back silently."""
    env = {
        "MINIMAX_API_KEY": "sk-same",
        "MINIMAX_PLAN_API_KEY": "sk-same",
        "DEEPSEEK_API_KEY": "sk-other",
        "http_proxy": "http://127.0.0.1:7899",
        "https_proxy": "http://127.0.0.1:7899",
    }
    found = ccp_doctor.duplicate_secrets(env)
    assert len(found) == 1
    assert "MINIMAX_API_KEY" in found[0] and "MINIMAX_PLAN_API_KEY" in found[0]
    assert "sk-same" not in found[0], "never print the secret itself"


def test_the_proxy_is_probed_not_assumed():
    live = socket.socket()
    live.bind(("127.0.0.1", 0))
    live.listen(1)
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    dead_port = closed.getsockname()[1]
    closed.close()
    try:
        alive, detail = ccp_doctor.proxy_port_alive(
            {"http_proxy": f"http://127.0.0.1:{live.getsockname()[1]}"}
        )
        assert alive and "accepts connections" in detail

        alive, _ = ccp_doctor.proxy_port_alive(
            {"http_proxy": f"http://127.0.0.1:{dead_port}"}
        )
        assert not alive, "a configured proxy that nobody listens on is a problem"
    finally:
        live.close()


def test_no_proxy_configured_is_not_a_failure():
    assert ccp_doctor.proxy_port_alive({}) == (True, "no proxy configured")


def test_probe_reports_the_model_that_actually_answered():
    seen = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"model": "qianfan-deepseek-v4.1-flash-1017"}).encode()

    def fake_urlopen(req, timeout=None):
        seen["body"] = json.loads(req.data)
        return FakeResponse()

    served, error = ccp_doctor.probe_model(
        "http://127.0.0.1:4000",
        "sk-bridge",
        "flash",
        urlopen=fake_urlopen,
    )
    assert error is None
    assert served == "qianfan-deepseek-v4.1-flash-1017"
    assert seen["body"]["max_tokens"] >= 2, "1 is rejected by some endpoints; it is not a routing signal"


def test_probe_failure_is_reported_not_swallowed():
    def boom(req, timeout=None):
        raise OSError("connection refused")

    served, error = ccp_doctor.probe_model(
        "http://127.0.0.1:4000", "sk-bridge", "flash", urlopen=boom
    )
    assert served is None
    assert "connection refused" in error


def test_the_1m_marker_is_not_a_different_model():
    """`foo[1m]` asks for the long-context deployment of `foo`; the served name
    comes back without the marker, which is not a fallback."""
    assert ccp_doctor.model_family("opencode-deepseek-v4.1-flash[1m]") == "opencode-deepseek-v4.1-flash"
    assert ccp_doctor.model_family("flash") == "flash"


def test_an_unreachable_bridge_is_named_instead_of_blaming_every_model(tmp_path, monkeypatch, capsys):
    """A bridge that is still starting is not a dead model (emily, 2026-11-04).

    `flash` exists only in the bridge's own list, so without this guard the
    doctor would report the profile, the active config and every bot as dead.
    """
    repo = tmp_path / "repo"
    (repo / "providers").mkdir(parents=True)
    (repo / "providers" / "qianfan.json.example").write_text(
        json.dumps({"env": {"ANTHROPIC_MODEL": "qianfan-deepseek-v4.1-flash"}})
    )
    providers = tmp_path / "providers"
    providers.mkdir()
    (providers / "bridged.json").write_text(json.dumps({"env": {"ANTHROPIC_MODEL": "flash"}}))
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"env": {"ANTHROPIC_MODEL": "flash"}}))
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text('model:\n  default: "flash"\n')
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    rc = ccp_doctor.main([
        "--providers-dir", str(providers),
        "--settings", str(settings),
        "--repo", str(repo),
        "--bridge", "http://127.0.0.1:9",
        "--bridge-attempts", "1",
        "--bridge-delay", "0",
        "--config-dir", str(tmp_path / "litellm"),
    ])
    out = capsys.readouterr().out
    assert rc == 1
    assert "did not answer /v1/models" in out
    assert "which the bridge no longer serves" not in out
