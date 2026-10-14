"""Guard rails for the registry + rendered outputs.

These tests catch the drift class that motivated the whole registry refactor:
duplicate model_names, missing context values, and profiles whose advertised
context drifts from the model actually mapped to the primary tier.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BRIDGE = REPO / "bridge"
REGISTRY = REPO / "registry"
sys.path.insert(0, str(BRIDGE))

import render_config  # noqa: E402

yaml = pytest.importorskip("yaml")


def _load():
    models = yaml.safe_load((REGISTRY / "models.yaml").read_text()) or {}
    profiles = yaml.safe_load((REGISTRY / "profiles.yaml").read_text()) or {}
    return models, profiles


def test_models_yaml_has_no_duplicate_names():
    doc, _ = _load()
    for p in doc.get("providers", []):
        names = [m["name"] for m in p["models"]]
        dupes = {n for n in names if names.count(n) > 1}
        assert not dupes, f"{p['name']} has duplicate model names: {dupes}"


def test_every_discovered_model_has_context():
    """Context must be auto-filled for the vast majority of models.

    Providers whose /models endpoint exposes metadata (cmdc, deepseek)
    are near-complete; providers that only return ids (cline, minimax) are
    filled from LiteLLM's map and may have a small unfilled tail. Either way
    every provider must clear 0.9 — no per-provider escape hatch, so a genuine
    gap (e.g. a whole family of models missing ctx) fails loudly instead of
    being papered over by a lowered floor.
    """
    doc, _ = _load()
    for p in doc.get("providers", []):
        total = len(p["models"])
        if not total:
            continue
        have = sum(1 for m in p["models"] if m.get("ctx"))
        assert have >= total * 0.9, f"{p['name']}: only {have}/{total} models have ctx"


def test_rendered_config_has_no_duplicate_model_names():
    doc, _ = _load()
    models, providers = render_config.index_models(doc)
    text = render_config.render_config(models, providers, _load()[1])
    names = []
    for line in text.splitlines():
        if line.strip().startswith("- model_name:"):
            names.append(line.split("model_name:", 1)[1].strip().strip("'"))
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"rendered config has duplicate model_name: {dupes}"


def test_clamp_map_covers_all_providers_with_output_cap():
    doc, profiles = _load()
    models, providers = render_config.index_models(doc)
    clamp = render_config.render_clamp(models, providers, profiles)
    # every cmdc model caps at 393216 -> must appear in the clamp map
    for (pname, name), meta in models.items():
        if pname == "cmdc" and meta.get("max_output"):
            assert clamp.get(name) == meta["max_output"]


def test_profile_context_matches_primary_tier_model():
    """The profile's CLAUDE_CODE_MAX_CONTEXT_TOKENS must equal the ctx of the
    model mapped to its primary tier — no more hand-typed magic numbers."""
    doc, profiles = _load()
    models, providers = render_config.index_models(doc)
    alias_idx = render_config._alias_index(profiles)
    for profile, spec in (profiles.get("profiles") or {}).items():
        pname = spec["provider"]
        rendered = render_config.render_profile(
            profile, spec, models, providers, alias_idx, profiles
        )
        obj = json.loads(rendered)
        primary = spec.get("primary_tier", "default")
        mname = spec["tiers"][primary]["model"]
        meta = models.get((pname, mname))
        if meta is None:
            meta = render_config.synth_meta(profiles, pname, mname)
        if not meta.get("ctx"):
            continue  # unknown context -> profile falls back to a default
        assert obj["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == str(int(meta["ctx"])), (
            f"{profile}: advertised ctx {obj['env']['CLAUDE_CODE_MAX_CONTEXT_TOKENS']} "
            f"!= model {mname} ctx {meta['ctx']}"
        )


def test_render_check_passes():
    """`render_config.py --check` must pass on a freshly rendered tree."""
    r = subprocess.run(
        [sys.executable, str(BRIDGE / "render_config.py")],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    r = subprocess.run(
        [sys.executable, str(BRIDGE / "render_config.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"rendered outputs are stale:\n{r.stdout}\n{r.stderr}"


# ── accounts (pools) and routing groups ───────────────────────────────────
# A pool is several deployments sharing one model_name: LiteLLM load-balances
# across them and cools down one that answers 429, which is what makes
# "move to the next account when a quota runs out" work without ccp owning a
# router. These tests pin the config shape that behaviour depends on.


def _render_with(profiles_doc):
    doc, _ = _load()
    models, providers = render_config.index_models(doc)
    for pname, pmeta in render_config._np_providers(profiles_doc).items():
        providers.setdefault(pname, {"auth_env": None, "api_base": None, **pmeta})
    return render_config.render_config(models, providers, profiles_doc)


def _deployments(text):
    """[(model_name, model, api_key_env or None)] in file order."""
    import re

    out = []
    for block in text.split("\n\n"):
        m = re.search(r"- model_name:\s*(.+)$", block, re.M)
        if not m:
            continue
        name = m.group(1).strip().strip("'")
        mm = re.search(r"^\s+model:\s*(.+)$", block, re.M)
        km = re.search(r"^\s+api_key:\s*os\.environ/(.+)$", block, re.M)
        out.append((name, mm.group(1).strip() if mm else "", km.group(1).strip() if km else None))
    return out


def test_no_accounts_renders_exactly_as_before():
    """The default registry must not grow a single extra deployment.

    Account pools are opt-in: an empty `accounts:` table has to be a no-op, or
    every existing install silently doubles its config.
    """
    doc, profiles = _load()
    assert not (profiles.get("accounts") or {}), "this test assumes no accounts configured"
    assert not (profiles.get("groups") or {}), "this test assumes no groups configured"
    text = _render_with(profiles)
    names = [n for n, _, _ in _deployments(text)]
    assert len(names) == len(set(names)), "no pool means no duplicate model_name"
    assert "router_settings" not in text, "nothing to route without groups"


def test_account_becomes_a_second_deployment_under_the_same_name():
    """An account must NOT fork the model name.

    `cline-deepseek-a2` as its own name would give no failover at all: the
    client sends `cline-deepseek`, gets the primary, and never learns the second
    account exists. Both accounts are deployments of `cline-deepseek`, which is
    what lets LiteLLM move to the second when the first answers 401/429.
    """
    doc, profiles = _load()
    profiles["accounts"] = {"cline": {"a2": {"auth_env": "CLINE_API_KEY_2"}}}
    deps = _deployments(_render_with(profiles))
    pool = [(m, k) for n, m, k in deps if n == "cline-deepseek"]
    assert len(pool) == 2, "one deployment per account"
    assert {k for _, k in pool} == {"CLINE_API_KEY", "CLINE_API_KEY_2"}
    assert len({m for m, _ in pool}) == 1, "same upstream model"
    # no suffixed name is invented anywhere
    assert not [n for n, _, _ in deps
                if n.startswith("cline-deepseek-") and n.endswith("a2")]


def test_account_pool_also_covers_the_1m_bracket_spelling():
    """Claude Code sends `cline-deepseek[1m]`, so the pool must exist under that
    name too — otherwise a 1M session gets no failover."""
    doc, profiles = _load()
    profiles["accounts"] = {"cline": {"a2": {"auth_env": "CLINE_API_KEY_2"}}}
    text = _render_with(profiles)
    deps = _deployments(text)
    bracketed = [(m, k) for n, m, k in deps if n == "cline-deepseek[1m]"]
    assert len(bracketed) == 2
    assert {k for _, k in bracketed} == {"CLINE_API_KEY", "CLINE_API_KEY_2"}
    # and the file is still valid YAML (quoting must wrap the whole name)
    parsed = yaml.safe_load(text)
    assert len(parsed["model_list"]) == len(deps)


def test_group_is_one_model_name_with_several_deployments():
    """A group must repeat its model_name — that repetition IS the load
    balancing. Deduping it (as the single-name guard would) silently collapses
    the group to one member."""
    doc, profiles = _load()
    profiles["groups"] = {
        "daily": {
            "members": ["cline-deepseek", "qianfan-deepseek-flash", "trae-glm"],
            "routing_strategy": "usage-based-routing-v2",
            "fallback": ["qianfan-deepseek-pro"],
        }
    }
    deps = _deployments(_render_with(profiles))
    group = [(n, m, k) for n, m, k in deps if n == "daily"]
    assert len(group) == 3, f"expected 3 group deployments, got {group}"
    # three *different* upstreams, each keeping its provider's key
    assert len({m for _, m, _ in group}) == 3
    assert {k for _, _, k in group} == {
        "CLINE_API_KEY", "QIANFAN_API_KEY", "TRAECLI_PERSONAL_ACCESS_TOKEN"}


def test_group_members_join_their_account_pools():
    """Every (member, account) pair is one deployment of the SAME group name.

    Splitting accounts into `daily-a2` would force the client to know which
    account to try, defeating the point of the pool.
    """
    doc, profiles = _load()
    profiles["accounts"] = {"cline": {"a2": {"auth_env": "CLINE_API_KEY_2"}}}
    profiles["groups"] = {"daily": {"members": ["cline-deepseek",
                                                "qianfan-deepseek-flash"]}}
    deps = _deployments(_render_with(profiles))
    names = [n for n, _, _ in deps]
    # 2 members, one of which has 2 accounts = 3 deployments, all one name
    assert names.count("daily") == 3
    assert not [n for n in names if n.startswith("daily-")], "accounts must not fork the name"
    keys = sorted(k for n, _, k in deps if n == "daily")
    assert keys == ["CLINE_API_KEY", "CLINE_API_KEY_2", "QIANFAN_API_KEY"]


def test_group_emits_router_settings_that_litellm_accepts():
    doc, profiles = _load()
    profiles["groups"] = {
        "daily": {
            "members": ["cline-deepseek", "qianfan-deepseek-flash"],
            "routing_strategy": "usage-based-routing-v2",
            "fallback": ["qianfan-deepseek-pro"],
        }
    }
    parsed = yaml.safe_load(_render_with(profiles))
    rs = parsed["router_settings"]
    assert rs["fallbacks"] == [{"daily": ["qianfan-deepseek-pro"]}]
    rg = rs["routing_groups"][0]
    assert rg["group_name"] == "daily"
    # the group claims exactly the one model_name its deployments share
    assert rg["models"] == ["daily"]
    assert rg["routing_strategy"] == "usage-based-routing-v2"
    # every group name it references must exist in model_list
    known = {m["model_name"] for m in parsed["model_list"]}
    assert "daily" in known
    for target in rs["fallbacks"][0]["daily"]:
        assert target in known, f"fallback {target} has no deployment"


def test_group_member_may_be_an_alias_a_registry_id_or_provider_slash_model():
    """All three spellings resolve. Distinct upstreams stay distinct; two
    members that resolve to the *same* upstream collapse to one deployment
    (the pool guard drops only exact duplicates, not distinct models)."""
    doc, profiles = _load()
    profiles["groups"] = {
        "g": {
            "members": [
                "qianfan-deepseek-flash",              # alias
                "MiniMax-M3",                          # bare registry id (minimax)
                "cline/deepseek/deepseek-v4.1-flash",  # provider/model
            ]
        }
    }
    deps = _deployments(_render_with(profiles))
    group = [m for n, m, _ in deps if n == "g"]
    assert len(group) == 3
    assert "openai/deepseek-v4.1-flash" in group           # qianfan (bridged)
    assert "anthropic/MiniMax-M3" in group                 # minimax (native)
    # cline keeps its subscription namespace: dropping the `cline-pass/` segment
    # silently bills pay-as-you-go instead of the flat plan (see
    # render_config.litellm_model_string_override).
    assert "openai/cline-pass/deepseek-v4.1-flash" in group


def test_group_dedupes_only_identical_deployments():
    """A bare id and an alias that resolve to the same provider+model are one
    deployment; the group must not emit the same target twice."""
    doc, profiles = _load()
    profiles["groups"] = {"g": {"members": ["cline-deepseek",
                                            "deepseek/deepseek-v4.1-flash"]}}
    deps = _deployments(_render_with(profiles))
    assert [n for n, _, _ in deps if n == "g"] == ["g"]


def test_unresolvable_group_member_is_reported_not_emitted(capsys):
    doc, profiles = _load()
    profiles["groups"] = {"g": {"members": ["cline-deepseek", "no-such-model-xyz"]}}
    deps = _deployments(_render_with(profiles))
    assert [n for n, _, _ in deps if n == "g"] == ["g"]  # only the resolvable one
    assert "no-such-model-xyz" in capsys.readouterr().err


def test_native_profile_sends_the_upstream_model_id_not_the_alias():
    doc, profiles = _load()
    models, providers = render_config.index_models(doc)
    # opendesign has an alias whose name differs from the upstream id
    assert profiles["aliases"]["opendesign"]["model"] == "deepseek-v4.1-flash"
    rendered = render_config.render_profile(
        "opendesign-deepseek-v4.1-flash",
        profiles["profiles"]["opendesign-deepseek-v4.1-flash"],
        models,
        providers,
        render_config._alias_index(profiles),
        profiles,
    )
    env = json.loads(rendered)["env"]
    assert env["ANTHROPIC_BASE_URL"] == "https://amr-link.open-design.ai"
    for key in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
        assert "opendesign" not in env[key], f"{key} leaks the ccp alias upstream: {env[key]}"


def test_native_profile_never_carries_the_1m_marker():
    """`[1m]` is stripped by the watchdog on the bridged path; a native endpoint
    has no watchdog, and a strict one rejects the suffixed id outright."""
    doc, profiles = _load()
    models, providers = render_config.index_models(doc)
    alias_idx = render_config._alias_index(profiles)
    for name, spec in profiles["profiles"].items():
        if not spec.get("native"):
            continue
        env = json.loads(
            render_config.render_profile(name, spec, models, providers, alias_idx, profiles)
        )["env"]
        for key, value in env.items():
            if key.endswith("MODEL") or key == "ANTHROPIC_MODEL":
                assert "[1m]" not in value, f"{name}.{key} carries [1m]: {value}"


def test_bridged_profiles_still_use_alias_and_1m_marker():
    """The native carve-out must not leak into the bridged path, where both
    conventions are required: the alias is the LiteLLM model_name and the marker
    is stripped by the watchdog."""
    doc, profiles = _load()
    models, providers = render_config.index_models(doc)
    alias_idx = render_config._alias_index(profiles)
    env = json.loads(
        render_config.render_profile(
            "qianfan-deepseek-v4.1-flash", profiles["profiles"]["qianfan-deepseek-v4.1-flash"], models, providers,
            alias_idx, profiles
        )
    )["env"]
    assert env["ANTHROPIC_MODEL"] == "qianfan-deepseek-flash[1m]"
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4000"


def test_opendesign_is_registered_and_discovered():
    """The provider must declare itself discoverable and native, and its tiers
    must resolve to ids the catalogue actually lists."""
    doc, profiles = _load()
    models, _ = render_config.index_models(doc)
    assert profiles["profiles"]["opendesign-deepseek-v4.1-flash"]["native"] is True
    known = {m for (p, m) in models if p == "opendesign"}
    assert known, "opendesign models missing from registry/models.yaml"
    for tier, tspec in profiles["profiles"]["opendesign-deepseek-v4.1-flash"]["tiers"].items():
        assert tspec["model"] in known, f"tier {tier} -> {tspec['model']} not in the catalogue"



# ── request rewrites ──────────────────────────────────────────────────────
# The one rule ccp ever shipped — codebuddy's `sanitize_channel` client check —
# was archived with that provider, so the table is empty. These tests pin that:
# an empty table must stay a no-op, and must not start rewriting requests for
# the providers that remain.


def _emitted_and_rules():
    """Render, then report (provider -> emitted names, expanded request rules)."""
    doc, profiles = _load()
    models, providers = render_config.index_models(doc)
    emitted: dict[str, set[str]] = {}
    render_config.render_config(models, providers, profiles, emitted)
    return models, profiles, emitted, render_config.render_request_rules(profiles, emitted)


def test_request_rules_are_empty_and_rewrite_nothing():
    """Nothing in the kept set may have its request rewritten.

    Stripping Anthropic's billing header anywhere but the gateway that demands
    it is a silent prompt mutation, so an empty table has to mean *empty*.
    """
    from sse_finalize import norm_model_key

    models, profiles, emitted, rules = _emitted_and_rules()
    assert (profiles.get("request_rules") or {}) == {}
    assert rules.get("sanitize_channel") in (None, [])
    # non-vacuous: providers really did emit names to check against
    assert any(names for names in emitted.values()), "nothing was emitted at all"
    listed = {norm_model_key(n) for n in (rules.get("sanitize_channel") or [])}
    for pname, names in emitted.items():
        for name in names:
            assert norm_model_key(name) not in listed, f"{pname}/{name} would be rewritten"
