"""Guard rails for the account/group curation layer and the quota reader.

`ccp accounts` edits registry/profiles.yaml, which is mostly comments — it is
the documentation for the curation layer. A YAML round-trip would delete all of
it, so the writer edits the text in place, and these tests pin that.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bridge"))

import ccp_accounts as ca  # noqa: E402
import ccp_quota as cq  # noqa: E402

yaml = pytest.importorskip("yaml")


# ── editing profiles.yaml without destroying its comments ─────────────────


SAMPLE = """\
# top comment
version: 1

# a comment about accounts
accounts: {}

# ── groups ─────────────────────────────
#   groups:
#     daily:
#       members: [a, b]
groups: {}

aliases:
  x: {provider: p, model: m}
"""


def test_replacing_a_key_keeps_every_comment():
    out = ca._replace_top_level_key(SAMPLE, "accounts", "accounts:\n  p:\n    a2: {}\n")
    assert out.count("#") == SAMPLE.count("#"), "comments must survive"
    assert "a comment about accounts" in out
    assert "members: [a, b]" in out
    assert yaml.safe_load(out)["accounts"] == {"p": {"a2": {}}}


def test_a_column_zero_comment_ends_the_block():
    """The block is the key plus its *indented* lines only.

    Treating a following column-0 comment as part of the block deleted the
    documentation sitting above the next key.
    """
    out = ca._replace_top_level_key(SAMPLE, "accounts", "accounts:\n  p: {}\n")
    assert "# ── groups ──" in out
    assert "groups: {}" in out
    assert yaml.safe_load(out)["groups"] == {}


def test_replacing_the_last_key_keeps_what_follows():
    out = ca._replace_top_level_key(SAMPLE, "aliases", "aliases: {}\n")
    assert yaml.safe_load(out)["aliases"] == {}
    assert "groups: {}" in out


def test_adding_a_missing_key_appends_it():
    out = ca._replace_top_level_key("version: 1\n", "accounts", "accounts:\n  p: {}\n")
    doc = yaml.safe_load(out)
    assert doc["version"] == 1 and doc["accounts"] == {"p": {}}


def test_write_registry_round_trips_through_the_real_file(tmp_path, monkeypatch):
    target = tmp_path / "profiles.yaml"
    target.write_text(SAMPLE)
    monkeypatch.setattr(ca, "PROFILES_YAML", target)

    ca.write_registry(
        {"qianfan": {"a2": {"auth_env": "QIANFAN_API_KEY_2"}}},
        {"daily": {"members": ["qianfan"], "routing_strategy": "least-busy"}},
    )
    accounts, groups = ca.load_registry()
    assert accounts == {"qianfan": {"a2": {"auth_env": "QIANFAN_API_KEY_2"}}}
    assert groups == {"daily": {"members": ["qianfan"], "routing_strategy": "least-busy"}}
    assert target.read_text().count("#") == SAMPLE.count("#")
    # no temp file left behind
    assert sorted(p.name for p in tmp_path.iterdir()) == ["profiles.yaml"]


# ── account commands ──────────────────────────────────────────────────────


def _args(**kw):
    base = {"provider": "qianfan", "suffix": "a2", "token": "tok", "label": None,
            "auth_env": None, "proxy_env": "/dev/null"}
    base.update(kw)
    return argparse.Namespace(**base)


def test_env_var_name_is_derived_from_provider_and_suffix():
    assert ca.env_var_for("qianfan", "a2") == "QIANFAN_API_KEY_A2"
    assert ca.env_var_for("cmdc", "edu") == "CMDC_API_KEY_EDU"


def test_add_writes_credential_and_registry(tmp_path, monkeypatch):
    target = tmp_path / "profiles.yaml"
    target.write_text(SAMPLE)
    env = tmp_path / "proxy.env"
    monkeypatch.setattr(ca, "PROFILES_YAML", target)
    monkeypatch.setattr(ca, "PROXY_ENV", env)

    assert ca.cmd_add(_args(proxy_env=str(env))) == 0
    accounts, _ = ca.load_registry()
    assert accounts["qianfan"]["a2"]["auth_env"] == "QIANFAN_API_KEY_A2"
    # the secret went to the env file, never into the committed registry
    assert "tok" not in target.read_text()
    assert "QIANFAN_API_KEY_A2=tok" in env.read_text()
    assert os.stat(env).st_mode & 0o777 == 0o600


def test_add_refuses_a_duplicate_suffix(tmp_path, monkeypatch):
    target = tmp_path / "profiles.yaml"
    target.write_text(SAMPLE)
    monkeypatch.setattr(ca, "PROFILES_YAML", target)
    monkeypatch.setattr(ca, "PROXY_ENV", tmp_path / "proxy.env")
    assert ca.cmd_add(_args(proxy_env=str(tmp_path / "proxy.env"))) == 0
    with pytest.raises(ca.AccountsError, match="already has an account"):
        ca.cmd_add(_args(proxy_env=str(tmp_path / "proxy.env")))


def test_add_rejects_a_suffix_that_would_break_the_env_var(tmp_path, monkeypatch):
    target = tmp_path / "profiles.yaml"
    target.write_text(SAMPLE)
    monkeypatch.setattr(ca, "PROFILES_YAML", target)
    monkeypatch.setattr(ca, "PROXY_ENV", tmp_path / "proxy.env")
    with pytest.raises(ca.AccountsError, match="bad suffix"):
        ca.cmd_add(_args(suffix="bad suffix!", proxy_env=str(tmp_path / "proxy.env")))


def test_remove_drops_the_account_but_leaves_the_secret_alone(tmp_path, monkeypatch):
    """ccp never rewrites a secrets file it cannot read; the same caution means
    it does not silently delete a credential either."""
    target = tmp_path / "profiles.yaml"
    target.write_text(SAMPLE)
    env = tmp_path / "proxy.env"
    monkeypatch.setattr(ca, "PROFILES_YAML", target)
    monkeypatch.setattr(ca, "PROXY_ENV", env)
    ca.cmd_add(_args(proxy_env=str(env)))
    assert ca.cmd_remove(_args(proxy_env=str(env))) == 0
    accounts, _ = ca.load_registry()
    assert not accounts, "the provider entry is dropped once its last account goes"
    assert "QIANFAN_API_KEY_A2=tok" in env.read_text(), "secret left for the user to remove"


# ── group commands ────────────────────────────────────────────────────────


def test_group_add_validates_the_strategy(tmp_path, monkeypatch):
    target = tmp_path / "profiles.yaml"
    target.write_text(SAMPLE)
    monkeypatch.setattr(ca, "PROFILES_YAML", target)
    ns = argparse.Namespace(name="daily", members=["qianfan"], strategy="nonsense",
                            fallback=None)
    with pytest.raises(ca.AccountsError, match="unknown strategy"):
        ca.cmd_group_add(ns)
    # ...and the file is untouched
    assert yaml.safe_load(target.read_text())["groups"] == {}


def test_group_add_records_members_and_fallback(tmp_path, monkeypatch):
    target = tmp_path / "profiles.yaml"
    target.write_text(SAMPLE)
    monkeypatch.setattr(ca, "PROFILES_YAML", target)
    ns = argparse.Namespace(name="daily", members=["qianfan", "cmdc-deepseek-fast"],
                            strategy="usage-based-routing-v2", fallback=["cmdc-deepseek-pro"])
    assert ca.cmd_group_add(ns) == 0
    _, groups = ca.load_registry()
    assert groups["daily"] == {
        "members": ["qianfan", "cmdc-deepseek-fast"],
        "routing_strategy": "usage-based-routing-v2",
        "fallback": ["cmdc-deepseek-pro"],
    }


# ── quota ─────────────────────────────────────────────────────────────────


def test_quota_cache_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(cq, "CACHE_DIR", tmp_path)
    assert cq.cached("qianfan") is None
    cq.store("qianfan", {"provider": "qianfan", "windows": []})
    assert cq.cached("qianfan")["provider"] == "qianfan"
    # an expired entry is ignored
    assert cq.cached("qianfan", max_age=-1) is None


def test_bar_is_proportional_and_clamped():
    assert cq._bar(0, 100, width=10) == "░" * 10
    assert cq._bar(100, 100, width=10) == "█" * 10
    assert cq._bar(50, 100, width=10) == "█" * 5 + "░" * 5
    # over 100% must not overflow the bar
    assert cq._bar(250, 100, width=10) == "█" * 10
    # a zero total must not divide by zero
    assert cq._bar(1, 0, width=4) == " " * 4
