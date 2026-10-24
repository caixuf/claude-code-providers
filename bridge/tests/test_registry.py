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

    What has to hold is that an EMPTY table is a no-op — not that the shipped
    file is empty. Asserting the committed state made the test fail the moment
    anyone configured a group, so the tables are blanked here instead.
    """
    doc, profiles = _load()
    bare = dict(profiles)
    bare["accounts"] = {}
    bare["groups"] = {}
    text = _render_with(bare)
    names = [n for n, _, _ in _deployments(text)]
    assert len(names) == len(set(names)), "no pool means no duplicate model_name"
    assert "router_settings" not in text, "nothing to route without groups"


def test_shipped_groups_reference_models_that_resolve():
    """A member that does not resolve is dropped with a stderr line only.

    render_config prints `cannot resolve member` and moves on, so a typo would
    leave a group that silently has fewer deployments than it claims — and a
    fallback pointing at nothing. Check the shipped groups against the registry.
    """
    doc, profiles = _load()
    models, _ = render_config.index_models(doc)
    for gname, gspec in (profiles.get("groups") or {}).items():
        for member in (gspec.get("members") or []) + (gspec.get("fallback") or []):
            prov, _ = render_config._split_member(profiles, models, member)
            assert prov is not None, f"group {gname} references unknown model {member!r}"


def test_shipped_aliases_render_to_their_own_registry_model(capsys):
    """An alias that names a model its provider lacks is answered by another one.

    This shipped once: `trae-deepseek-v4-pro` pointed at `deepseek-V4-Pro` while
    the catalogue row was `DeepSeek-V4-Pro-Official`. `render_config` keys its
    lookup on the exact ``(provider, model)`` pair and, on a miss, prints
    `! alias …: model … not in registry` to stderr and moves on — the render
    still succeeds and the profile still loads, so nothing else notices.

    So the shipped table is checked through the render itself, which is the code
    path that does the dropping: every alias must emit a deployment carrying its
    *own* model's upstream string (not a same-provider sibling that merely shares
    the alias spelling), and none may be dropped with a warning. The expectation
    is derived from the alias table rather than pinned, so it keeps holding as
    aliases come and go. Note the registry key is exact — the trae adapter
    lowercases both sides for a user typing a name, but the case written in
    `profiles.yaml` is not forgiving, and that asymmetry is what let the bug slip.
    """
    doc, profiles = _load()
    models, _ = render_config.index_models(doc)

    upstreams: dict[str, set[str]] = {}
    for name, upstream, _ in _deployments(_render_with(profiles)):
        upstreams.setdefault(name, set()).add(upstream)

    problems = []
    for alias, spec in (profiles.get("aliases") or {}).items():
        if alias not in upstreams:
            problems.append(
                f"{alias} -> {spec['provider']}/{spec['model']} produced no deployment"
            )
            continue
        meta = models.get((spec["provider"], spec["model"]), {})
        want = render_config.litellm_model_string(spec["provider"], spec["model"], meta)
        if want not in upstreams[alias]:
            problems.append(f"{alias} emitted {sorted(upstreams[alias])}, expected {want!r}")
    assert not problems, "aliases did not render to their registry model:\n" + "\n".join(
        "  " + p for p in problems
    )
    assert "not in registry" not in capsys.readouterr().err


def test_the_chatgpt_endpoint_kind_keeps_its_native_prefix():
    """`endpoint_kind: chatgpt` must render `chatgpt/<id>`, not `openai/<id>`.

    ChatGPT OAuth is a LiteLLM-native provider: it owns the endpoint and the
    token store, so `openai/gpt-6.1-sol` would be sent to api.openai.com with no
    key. The kind is declared by a registry that is not in this repo — a host
    running its own `registry/models.yaml` — which is exactly why the renderer
    has to honour it here rather than only where it was first needed.
    """
    meta = {"endpoint_kind": "chatgpt"}
    assert render_config.litellm_model_string("chatgpt", "gpt-6.1-sol", meta) == \
        "chatgpt/gpt-6.1-sol"
    # Nothing else may drift with it.
    assert render_config.litellm_model_string("x", "m", {"endpoint_kind": "openai"}) == "openai/m"
    assert render_config.litellm_model_string("x", "m", {"endpoint_kind": "anthropic"}) == \
        "anthropic/m"


def test_account_becomes_a_second_deployment_under_the_same_name():
    """An account must NOT fork the model name.

    `cline-deepseek-v4.1-flash-a2` as its own name would give no failover at all: the
    client sends `cline-deepseek-v4.1-flash`, gets the primary, and never learns the second
    account exists. Both accounts are deployments of `cline-deepseek-v4.1-flash`, which is
    what lets LiteLLM move to the second when the first answers 401/429.
    """
    doc, profiles = _load()
    profiles["accounts"] = {"cline": {"a2": {"auth_env": "CLINE_API_KEY_2"}}}
    deps = _deployments(_render_with(profiles))
    pool = [(m, k) for n, m, k in deps if n == "cline-deepseek-v4.1-flash"]
    assert len(pool) == 2, "one deployment per account"
    assert {k for _, k in pool} == {"CLINE_API_KEY", "CLINE_API_KEY_2"}
    assert len({m for m, _ in pool}) == 1, "same upstream model"
    # no suffixed name is invented anywhere
    assert not [n for n, _, _ in deps
                if n.startswith("cline-deepseek-v4.1-flash-") and n.endswith("a2")]


def test_account_pool_also_covers_the_1m_bracket_spelling():
    """Claude Code sends `cline-deepseek-v4.1-flash[1m]`, so the pool must exist under that
    name too — otherwise a 1M session gets no failover."""
    doc, profiles = _load()
    profiles["accounts"] = {"cline": {"a2": {"auth_env": "CLINE_API_KEY_2"}}}
    text = _render_with(profiles)
    deps = _deployments(text)
    bracketed = [(m, k) for n, m, k in deps if n == "cline-deepseek-v4.1-flash[1m]"]
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
            "members": ["cline-deepseek-v4.1-flash", "opendesign-deepseek-v4.1-flash",
                        "trae-glm-5.3"],
            "routing_strategy": "usage-based-routing-v2",
            "fallback": ["opendesign-deepseek-v4-pro"],
        }
    }
    deps = _deployments(_render_with(profiles))
    group = [(n, m, k) for n, m, k in deps if n == "daily"]
    assert len(group) == 3, f"expected 3 group deployments, got {group}"
    # three *different* upstreams, each keeping its provider's key
    assert len({m for _, m, _ in group}) == 3
    assert {k for _, _, k in group} == {
        "CLINE_API_KEY", "OPENDESIGN_API_KEY", "TRAECLI_PERSONAL_ACCESS_TOKEN"}


def test_group_members_join_their_account_pools():
    """Every (member, account) pair is one deployment of the SAME group name.

    Splitting accounts into `daily-a2` would force the client to know which
    account to try, defeating the point of the pool.
    """
    doc, profiles = _load()
    profiles["accounts"] = {"cline": {"a2": {"auth_env": "CLINE_API_KEY_2"}}}
    profiles["groups"] = {"daily": {"members": ["cline-deepseek-v4.1-flash",
                                                "opendesign-deepseek-v4.1-flash"]}}
    deps = _deployments(_render_with(profiles))
    names = [n for n, _, _ in deps]
    # 2 members, one of which has 2 accounts = 3 deployments, all one name
    assert names.count("daily") == 3
    assert not [n for n in names if n.startswith("daily-")], "accounts must not fork the name"
    keys = sorted(k for n, _, k in deps if n == "daily")
    assert keys == ["CLINE_API_KEY", "CLINE_API_KEY_2", "OPENDESIGN_API_KEY"]


def test_group_emits_router_settings_that_litellm_accepts():
    doc, profiles = _load()
    profiles["groups"] = {
        "daily": {
            "members": ["cline-deepseek-v4.1-flash", "opendesign-deepseek-v4.1-flash"],
            "routing_strategy": "usage-based-routing-v2",
            "fallback": ["opendesign-deepseek-v4-pro"],
        }
    }
    parsed = yaml.safe_load(_render_with(profiles))
    rs = parsed["router_settings"]
    assert rs["fallbacks"] == [{"daily": ["opendesign-deepseek-v4-pro"]}]
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
                "cline-deepseek-v4.1-flash",           # alias
                "MiniMax-M3",                          # bare registry id (minimax)
                "opendesign/deepseek-v4.1-flash",      # provider/model
            ]
        }
    }
    deps = _deployments(_render_with(profiles))
    group = [m for n, m, _ in deps if n == "g"]
    assert len(group) == 3
    assert "anthropic/MiniMax-M3" in group                 # minimax (native)
    assert "anthropic/deepseek-v4.1-flash" in group        # opendesign (native)
    # cline keeps its subscription namespace: dropping the `cline-pass/` segment
    # silently bills pay-as-you-go instead of the flat plan (see
    # render_config.litellm_model_string_override).
    assert "openai/cline-pass/deepseek-v4.1-flash" in group


def test_group_dedupes_only_identical_deployments():
    """A bare id and an alias that resolve to the same provider+model are one
    deployment; the group must not emit the same target twice."""
    doc, profiles = _load()
    profiles["groups"] = {"g": {"members": ["cline-deepseek-v4.1-flash",
                                            "deepseek/deepseek-v4.1-flash"]}}
    deps = _deployments(_render_with(profiles))
    assert [n for n, _, _ in deps if n == "g"] == ["g"]


def test_unresolvable_group_member_is_reported_not_emitted(capsys):
    doc, profiles = _load()
    profiles["groups"] = {"g": {"members": ["cline-deepseek-v4.1-flash", "no-such-model-xyz"]}}
    deps = _deployments(_render_with(profiles))
    assert [n for n, _, _ in deps if n == "g"] == ["g"]  # only the resolvable one
    assert "no-such-model-xyz" in capsys.readouterr().err


def test_native_profile_sends_the_upstream_model_id_not_the_alias():
    doc, profiles = _load()
    models, providers = render_config.index_models(doc)
    # opendesign has an alias whose name differs from the upstream id
    assert profiles["aliases"]["opendesign-deepseek-v4.1-flash"]["model"] == "deepseek-v4.1-flash"
    rendered = render_config.render_profile(
        "opendesign",
        profiles["profiles"]["opendesign"],
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
            "cline", profiles["profiles"]["cline"], models, providers,
            alias_idx, profiles
        )
    )["env"]
    assert env["ANTHROPIC_MODEL"] == "cline-deepseek-v4.1-flash[1m]"
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4000"


def test_codex_is_native_and_answers_on_the_loopback():
    """Codex is a local gateway that speaks Anthropic, so nothing may be bridged.

    Two things are easy to get wrong here and neither fails loudly at runtime:
    a native profile must advertise the REAL window (272k — under 1M, so a
    `[1m]` marker or a 1048000 context would let Claude Code overrun it), and
    its tiers must not drift onto an id the gateway answers 503 for.
    """
    doc, profiles = _load()
    models, providers = render_config.index_models(doc)
    spec = profiles["profiles"]["codex"]
    assert spec["native"] is True
    assert providers["codex"]["api_base"].startswith("http://localhost:")

    known = {m for (p, m) in models if p == "codex"}
    assert known, "codex models missing from registry/models.yaml"
    for tier, tspec in spec["tiers"].items():
        assert tspec["model"] in known, f"tier {tier} -> {tspec['model']} not in the catalogue"

    env = json.loads(
        render_config.render_profile(
            "codex", spec, models, providers, render_config._alias_index(profiles), profiles
        )
    )["env"]
    assert env["ANTHROPIC_BASE_URL"] == providers["codex"]["api_base"]
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "272000"
    for key, val in env.items():
        assert "[1m]" not in str(val), f"{key} carries the bridged marker: {val}"


def test_opendesign_is_registered_and_discovered():
    """The provider must declare itself discoverable and native, and its tiers
    must resolve to ids the catalogue actually lists."""
    doc, profiles = _load()
    models, _ = render_config.index_models(doc)
    assert profiles["profiles"]["opendesign"]["native"] is True
    known = {m for (p, m) in models if p == "opendesign"}
    assert known, "opendesign models missing from registry/models.yaml"
    for tier, tspec in profiles["profiles"]["opendesign"]["tiers"].items():
        assert tspec["model"] in known, f"tier {tier} -> {tspec['model']} not in the catalogue"



# ── request rewrites ──────────────────────────────────────────────────────
# One rule is in force: `sanitize_channel`, for WorkBuddy alone. Its gateway
# demands that no foreign client identity reaches it
# (docs/workbuddy-channel-check.md), and Claude Code always sends one.
# Everywhere else the rewrite must not fire: stripping Anthropic's billing
# header anywhere but the gateway that asks for it is a silent prompt mutation.


def _emitted_and_rules():
    """Render, then report (provider -> emitted names, expanded request rules)."""
    doc, profiles = _load()
    models, providers = render_config.index_models(doc)
    emitted: dict[str, set[str]] = {}
    render_config.render_config(models, providers, profiles, emitted)
    return models, profiles, emitted, render_config.render_request_rules(profiles, emitted)


def test_sanitize_channel_is_scoped_to_the_provider_that_needs_it():
    """Only WorkBuddy's own model names may be rewritten."""
    from sse_finalize import norm_model_key

    models, profiles, emitted, rules = _emitted_and_rules()
    assert set(rules) == {"sanitize_channel"}, f"unexpected rules: {sorted(rules)}"
    listed = {norm_model_key(n) for n in (rules["sanitize_channel"] or [])}
    assert listed, "the rule covers nothing, so it would never fire"
    workbuddy_names = {norm_model_key(n) for n in emitted.get("workbuddy", ())}
    assert listed <= workbuddy_names, f"rewritten but not WorkBuddy's: {listed - workbuddy_names}"
    for pname, names in emitted.items():
        if pname == "workbuddy":
            continue
        for name in names:
            assert norm_model_key(name) not in listed, f"{pname}/{name} would be rewritten"
    # non-vacuous: providers really did emit names to check against
    assert any(names for names in emitted.values()), "nothing was emitted at all"


def test_workbuddy_deployments_carry_the_conversation_headers():
    """The channel check reads three headers; they must be on every deployment."""
    doc, profiles = _load()
    models, providers = render_config.index_models(doc)
    text = render_config.render_config(models, providers, profiles)
    cfg = yaml.safe_load(text)
    deployments = [
        d for d in cfg.get("model_list", [])
        if str(d.get("litellm_params", {}).get("api_base", "")).startswith("https://www.codebuddy.ai")
    ]
    assert deployments, "no WorkBuddy deployment was rendered"
    for d in deployments:
        eh = d["litellm_params"].get("extra_headers") or {}
        for h in ("X-Conversation-ID", "X-Conversation-Message-ID", "X-Conversation-Request-ID"):
            assert h in eh, f"{d['model_name']} is missing {h}"


def test_rendered_config_has_drop_params_setting():
    """LiteLLM must drop unsupported params (like OpenAI 'store') when routing."""
    doc, profiles = _load()
    models, providers = render_config.index_models(doc)
    text = render_config.render_config(models, providers, profiles)
    cfg = yaml.safe_load(text)
    assert cfg.get("litellm_settings", {}).get("drop_params") is True



def test_workbuddy_host_follows_the_login_not_the_registry(tmp_path, monkeypatch):
    """WorkBuddy runs two gateways; a session is valid at exactly one of them.

    The registry names the international one, but a CN account must be rendered
    against the CN gateway — otherwise it is refused with a bare 401 that looks
    like an expired login. `ccp auth workbuddy` writes the login's own base
    into proxy.env and the render reads it back.
    """
    home = tmp_path / "home"
    (home / ".config" / "litellm").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    env_file = home / ".config" / "litellm" / "proxy.env"
    env_file.write_text(
        "WORKBUDDY_API_KEY=token\nWORKBUDDY_API_BASE=https://copilot.tencent.com/v2\n"
    )
    doc, profiles = _load()
    models, providers = render_config.index_models(doc)
    assert render_config._env_api_base("workbuddy", providers) == "https://copilot.tencent.com/v2"
    text = render_config.render_config(models, providers, profiles)
    cfg = yaml.safe_load(text)
    bases = {
        d["litellm_params"].get("api_base")
        for d in cfg.get("model_list", [])
        if d["litellm_params"].get("api_base", "").startswith("https://")
    }
    assert "https://copilot.tencent.com/v2" in bases, bases
    assert "https://www.codebuddy.ai/v2" not in bases, bases
