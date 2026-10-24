"""The Hermes profile is a hand-written, commented file — these tests pin that.

Two failure modes are easy to ship and hard to notice: a YAML round trip that
silently eats every comment in the profile, and a model name the bridge never
emitted, which surfaces as a 400 that reads like the bridge is down. Both are
asserted here.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BRIDGE = REPO / "bridge"
REGISTRY = REPO / "registry"
sys.path.insert(0, str(BRIDGE))

import hermes_config  # noqa: E402
import render_config  # noqa: E402

yaml = pytest.importorskip("yaml")


def _load():
    models = yaml.safe_load((REGISTRY / "models.yaml").read_text()) or {}
    profiles = yaml.safe_load((REGISTRY / "profiles.yaml").read_text()) or {}
    return models, profiles


# Mirrors the shape of a real profile: comments above and inside, a sibling
# provider that must not be touched, and two lines carrying the model name.
PROFILE = """\
# 主模型走本机 ccp（LiteLLM 代理 127.0.0.1:4000）。
# 第二行注释也要原样保留。
model:
  default: main
  provider: custom:ccp

fallback_model:
  - provider: custom:imfl
    model: MiniMax-M3.1-Flash

providers:
  ccp:
    name: ccp
    base_url: "http://127.0.0.1:4000/v1"
    api_mode: chat_completions
    api_key: "sk-cmdc-litellm-local"
    default_model: main
  imfl:
    name: imfl
    base_url: "https://api.minimaxi.com/v1"
    key_env: AUTOTEST_LLM_API_KEY
    default_model: "MiniMax-M3.1-Flash"

compression:
  enabled: true
  threshold: 0.70
"""


def _emitted_names() -> set[str]:
    """Every model_name the proxy actually serves."""
    doc, profiles = _load()
    models, providers = render_config.index_models(doc)
    for pname, pdef in render_config._np_providers(profiles).items():
        providers.setdefault(pname, pdef)
    text = render_config.render_config(models, providers, profiles)
    return {m.strip().strip("'\"") for m in re.findall(r"model_name:\s*(\S+)", text)}


def test_it_changes_only_the_two_lines_that_carry_the_model():
    updated, seen = hermes_config.rewrite(PROFILE, "codex-gpt-6.1-sol")

    assert seen["model"] == "main"
    assert seen["provider"] == "custom:ccp"
    assert seen["default_model"] == "main"

    before, after = PROFILE.splitlines(), updated.splitlines()
    assert len(before) == len(after)
    differing = [i for i, (b, a) in enumerate(zip(before, after)) if b != a]
    assert differing == [3, 16], "only `model.default` and `providers.ccp.default_model` may move"
    assert after[3] == "  default: codex-gpt-6.1-sol"
    assert after[16] == "    default_model: codex-gpt-6.1-sol"
    # Everything else is byte-identical, comments included.
    assert [l for i, l in enumerate(after) if i not in differing] == \
           [l for i, l in enumerate(before) if i not in differing]


def test_the_sibling_provider_and_the_comment_block_survive():
    updated, _ = hermes_config.rewrite(PROFILE, "trae-deepseek-v4.1-flash")

    assert "# 第二行注释也要原样保留。" in updated
    assert '    default_model: "MiniMax-M3.1-Flash"' in updated, "imfl's model must not move"
    assert "threshold: 0.70" in updated
    # The result still parses as the same document, with only the one value moved.
    doc = yaml.safe_load(updated)
    assert doc["model"]["default"] == "trae-deepseek-v4.1-flash"
    assert doc["providers"]["ccp"]["default_model"] == "trae-deepseek-v4.1-flash"
    assert doc["providers"]["imfl"]["default_model"] == "MiniMax-M3.1-Flash"


def test_reading_without_a_name_changes_nothing():
    updated, seen = hermes_config.rewrite(PROFILE)
    assert updated == PROFILE
    assert seen == {"model": "main", "provider": "custom:ccp", "default_model": "main"}


def test_a_last_line_without_a_newline_does_not_gain_one():
    """A rewrite may not append a newline the file never had.

    The contract is "two lines change, nothing else" — and a file that ends on
    the `default_model` line would otherwise grow a byte on every switch.
    """
    minimal = "model:\n  default: main\n  provider: custom:ccp\nproviders:\n  ccp:\n    default_model: main"
    updated, _ = hermes_config.rewrite(minimal, "trae-deepseek-v4.1-flash")
    assert not updated.endswith("\n")
    assert updated.endswith("default_model: trae-deepseek-v4.1-flash")
    assert updated.count("\n") == minimal.count("\n")


def test_a_folded_or_quoted_value_keeps_its_quoting():
    text = PROFILE.replace("  default: main", '  default: "main"')
    updated, _ = hermes_config.rewrite(text, "codex-gpt-6.1-sol")
    assert '  default: "codex-gpt-6.1-sol"' in updated


@pytest.mark.parametrize("provider", ["cline", "codex", "deepseek", "minimax", "opendesign", "qianfan", "trae"])
def test_every_provider_resolves_to_a_name_the_proxy_actually_emits(provider):
    """The whole point of the command: a name that a deployment answers.

    A name the registry knows but `render` never emitted would be a 400 at
    runtime, indistinguishable from the bridge being down.
    """
    info = hermes_config.resolve(provider, None)
    assert info["name"] in _emitted_names(), f"{provider} -> {info['name']} is not served"


def test_a_provider_without_an_alias_uses_the_registry_id():
    """Only aliased providers get a `provider-model` name; the rest are bare.

    Minimax and Deepseek have no entries in the alias table, and inventing one
    (`minimax-MiniMax-M3.1-Flash`) would name a deployment that does not exist.
    """
    assert hermes_config.resolve("minimax", None)["name"] == "MiniMax-M3.1-Flash"
    assert hermes_config.resolve("deepseek", None)["name"] == "deepseek-flash"
    # ...while an aliased one prefers the short name, never the upstream id.
    assert hermes_config.resolve("codex", None)["name"] == "codex-gpt-6.1-sol"


def test_an_explicit_model_in_the_catalogue_resolves_case_insensitively():
    info = hermes_config.resolve("codex", "GPT-6-LUNA")
    assert info["name"] == "codex-gpt-6-luna"
    assert info["registry_model"] == "gpt-6-luna", "the registry's spelling is what is written"


def test_a_profile_on_another_provider_without_a_ccp_block_is_refused(tmp_path):
    """Nothing is written when there is no ccp entry point to move the bot to.

    The profile has to actually be missing the block: an earlier version of this
    test moved `model.provider` but left `providers.ccp` in the fixture, so it
    asserted a refusal the code had no reason to make.
    """
    bad = tmp_path / "config.yaml"
    text = (
        PROFILE
        .replace("provider: custom:ccp", "provider: custom:imfl")
        .replace("    default_model: main\n", "")
    )
    bad.write_text(text)
    with pytest.raises(SystemExit) as e:
        hermes_config.main(["render", "codex", "--config", str(bad)])
    assert "not custom:ccp" in str(e.value)
    assert "base_url" in str(e.value), "the message must carry the block to paste"
    assert bad.read_text() == text, "a refusal must not write anything"


def test_a_bot_still_on_an_old_provider_moves_all_the_way(tmp_path):
    """The bot that has to move is the one this command exists for.

    Refusing a profile on `commandcode` left hand-editing as the only way across.
    Moving it means moving `model.provider` too: that key is what picks the entry
    point, so writing only the model name under `providers.ccp` would leave the
    bot talking to commandcode — a rewrite that reads like a switch and behaves
    like a no-op.
    """
    cfg = tmp_path / "config.yaml"
    cfg.write_text(PROFILE.replace("provider: custom:ccp", "provider: commandcode"))
    hermes_config.main(["render", "codex", "--config", str(cfg), "--no-backup"])
    doc = yaml.safe_load(cfg.read_text())
    assert doc["model"]["provider"] == "custom:ccp", "the entry point must move, not just the name"
    assert doc["model"]["default"] == "codex-gpt-6.1-sol"
    assert doc["providers"]["ccp"]["default_model"] == "codex-gpt-6.1-sol"
    assert doc["providers"]["imfl"]["default_model"] == "MiniMax-M3.1-Flash"


def test_a_missing_ccp_block_says_which_block_to_add(tmp_path):
    bad = tmp_path / "config.yaml"
    bad.write_text(PROFILE.replace("    default_model: main\n", ""))
    with pytest.raises(SystemExit) as e:
        hermes_config.main(["render", "codex", "--config", str(bad)])
    assert "providers.ccp.default_model" in str(e.value)
    assert "base_url" in str(e.value), "the message must carry the block to paste"


def test_an_unknown_model_lists_the_catalogue(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(PROFILE)
    with pytest.raises(SystemExit) as e:
        hermes_config.main(["render", "codex", "gpt-9", "--config", str(cfg)])
    assert "gpt-6.1-sol" in str(e.value)


def test_a_dry_run_writes_nothing(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(PROFILE)
    assert hermes_config.main(["render", "codex", "--config", str(cfg), "--dry-run"]) == 0
    assert cfg.read_text() == PROFILE
    assert "CHANGED=0" in capsys.readouterr().out


def test_rerunning_is_a_no_op(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(PROFILE)
    hermes_config.main(["render", "codex", "--config", str(cfg), "--no-backup"])
    written = cfg.read_text()
    assert "CHANGED=1" in capsys.readouterr().out
    hermes_config.main(["render", "codex", "--config", str(cfg), "--no-backup"])
    out = capsys.readouterr().out
    assert "CHANGED=0" in out and "already up to date" in out
    assert cfg.read_text() == written


def test_a_rewrite_backs_the_profile_up(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(PROFILE)
    hermes_config.main(["render", "codex", "--config", str(cfg)])
    backups = list(tmp_path.glob("config.yaml.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text() == PROFILE, "the backup must be the file as it was"


def test_the_profile_name_is_reported_so_the_unit_can_be_restarted(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(PROFILE)
    hermes_config.main(["render", "codex", "--config", str(cfg), "--no-backup", "--profile", "imfl"])
    assert re.search(r"^PROFILE=imfl$", capsys.readouterr().out, re.M)

def _install_gateway(home: Path, name: str) -> None:
    units = home / ".config" / "systemd" / "user"
    units.mkdir(parents=True, exist_ok=True)
    (units / f"hermes-gateway-{name}.service").write_text("[Unit]\n")


def test_the_default_profile_is_the_one_the_gateway_runs(tmp_path, monkeypatch):
    """`ccp hermes` with no --profile must ask about the live bot."""
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    _install_gateway(tmp_path, "caixuf")
    assert hermes_config.default_profile() == "caixuf"

    monkeypatch.setenv("HERMES_PROFILE", "deepseek")
    assert hermes_config.default_profile() == "deepseek", "$HERMES_PROFILE outranks the discovery"


def test_active_profile_is_not_the_bot(tmp_path, monkeypatch):
    """`active_profile` is the sticky default for a *bare* `hermes` command.

    It can name a profile with no gateway at all. On this workstation it said
    `ccp` while the running bot was `caixuf` — following it would rewrite a
    profile that passes every check this command makes and then bounce a
    `hermes-gateway-ccp.service` that does not exist, leaving the real bot
    untouched with no error anywhere in the output.
    """
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".hermes").mkdir()
    (tmp_path / ".hermes" / "active_profile").write_text("ccp\n")
    _install_gateway(tmp_path, "caixuf")
    assert hermes_config.default_profile() == "caixuf", "the bot wins, not the CLI default"


def test_the_bot_wins_over_a_headless_gateway(tmp_path, monkeypatch):
    """A gateway with every platform off is not a bot.

    This host really has two installed (`caixuf` serving Feishu, `ccp` serving
    nothing but cron and the local API). Counting units made the bare command
    give up on a machine that has exactly one bot on it.
    """
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    for name in ("caixuf", "ccp"):
        _install_gateway(tmp_path, name)
    bots = {"caixuf": "platforms:\n  feishu:\n    enabled: true\n",
            "ccp": "platforms:\n  telegram:\n    enabled: false\n"}
    for name, text in bots.items():
        d = tmp_path / ".hermes" / "profiles" / name
        d.mkdir(parents=True)
        (d / "config.yaml").write_text(text)
    assert hermes_config.default_profile() == "caixuf"


def test_two_bots_ask_instead_of_guessing(tmp_path, monkeypatch):
    """A guess here writes to a live bot's profile. Ambiguity must be an error."""
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    for name in ("caixuf", "other"):
        _install_gateway(tmp_path, name)
        d = tmp_path / ".hermes" / "profiles" / name
        d.mkdir(parents=True)
        (d / "config.yaml").write_text("platforms:\n  feishu:\n    enabled: true\n")
    assert hermes_config.default_profile() is None


def test_no_profile_anywhere_is_an_error_not_a_guess(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert hermes_config.default_profile() is None
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "profiles" / "ccp").mkdir(parents=True)
    with pytest.raises(SystemExit) as e:
        hermes_config._require_profile(hermes_config.argparse.Namespace(config=None, profile=None))
    assert "--profile" in str(e.value) and "ccp" in str(e.value), "the message must name the ones that exist"
