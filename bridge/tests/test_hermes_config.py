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


def test_a_profile_not_pointed_at_ccp_is_refused(tmp_path):
    bad = tmp_path / "config.yaml"
    bad.write_text(PROFILE.replace("provider: custom:ccp", "provider: custom:imfl"))
    with pytest.raises(SystemExit) as e:
        hermes_config.main(["render", "codex", "--config", str(bad)])
    assert "not custom:ccp" in str(e.value)
    assert bad.read_text() == PROFILE.replace("provider: custom:ccp", "provider: custom:imfl")


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

def test_the_default_profile_is_the_one_the_bot_runs(tmp_path, monkeypatch):
    """`ccp hermes` with no --profile must ask about the live bot.

    A gateway unit only says a profile is *installed*; several can be, and the
    run that gets the messages is the one `active_profile` names. Reading the
    unit instead answered for a bot nobody was talking to.
    """
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "active_profile").write_text("imfl\n")
    assert hermes_config.default_profile() == "imfl", "the trailing newline must not be part of the name"
    (tmp_path / "active_profile").write_text("ccp")
    assert hermes_config.default_profile() == "ccp"
    monkeypatch.setenv("HERMES_PROFILE", "deepseek")
    assert hermes_config.default_profile() == "deepseek", "$HERMES_PROFILE outranks the file"


def test_no_profile_anywhere_is_an_error_not_a_guess(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert hermes_config.default_profile() is None
    (tmp_path / "profiles" / "ccp").mkdir(parents=True)
    cfg = hermes_config.profile_config("ccp")
    with pytest.raises(SystemExit) as e:
        hermes_config._require_profile(hermes_config.argparse.Namespace(config=None, profile=None))
    assert "--profile" in str(e.value) and "ccp" in str(e.value), "the message must name the ones that exist"
