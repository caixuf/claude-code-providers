"""Tests for bridge/ccp_pick.py — the provider-as-folder resolution.

The distinction under test is the one that silently breaks model switching: a
*native* provider is addressed by its upstream id, while a *bridged* one is
addressed by the alias that LiteLLM actually accepts. Getting it wrong writes a
model name the endpoint rejects, and the session only fails later.
"""

import io
import json
import os
import pathlib
import subprocess
import sys

import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "bridge"))
import ccp_pick as cp  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
PICK = REPO / "bridge" / "ccp_pick.py"
PROFILES = REPO / "registry" / "profiles.yaml"
MODELS = REPO / "registry" / "models.yaml"

sys.path.insert(0, str(REPO / "bridge"))
import ccp_pick  # noqa: E402


@pytest.fixture(scope="module")
def docs():
    return (
        yaml.safe_load(MODELS.read_text()) or {},
        yaml.safe_load(PROFILES.read_text()) or {},
    )


@pytest.fixture(scope="module")
def providers(docs):
    models_doc, profiles_doc = docs
    return {p.name: p for p in ccp_pick._providers(profiles_doc, models_doc)}


def _registry_provider_names():
    """Provider names `ccp folders` is expected to print, from the registry."""
    models_doc = yaml.safe_load(MODELS.read_text()) or {}
    profiles_doc = yaml.safe_load(PROFILES.read_text()) or {}
    return {p.name for p in ccp_pick._providers(profiles_doc, models_doc)}


def run(*args):
    out = subprocess.run(
        [sys.executable, str(PICK), *args], capture_output=True, text=True
    )
    return out.returncode, out.stdout, out.stderr


def kv(stdout):
    return dict(
        line.split("=", 1) for line in stdout.strip().splitlines() if "=" in line
    )


# ── the folder list ──────────────────────────────────────────────────────

def test_every_profile_is_a_provider_folder(providers):
    """A profile is a provider, not a model: `ccp` shows one line per provider."""
    names = set(providers)
    expected = set(yaml.safe_load(PROFILES.read_text())["profiles"])
    assert names == expected
    for name in names:
        assert "-" not in name or name in {"opendesign"}, f"{name} is not a provider name"


def test_each_folder_exposes_every_model_of_that_provider(docs, providers):
    """Nothing advertised by the registry is unreachable from its own folder.

    This is what the old one-profile-per-model layout broke: the provider had a
    catalogue but `ccp` showed one entry, so the other models were unreachable.
    """
    models_doc, _ = docs
    for prov in models_doc["providers"]:
        p = providers[prov["name"]]
        names = {m["name"] for m in prov["models"]}
        assert len(p.models) == len(names), f"{prov['name']}: folder is incomplete"


def test_bridged_folders_expose_their_aliases_not_upstream_ids(providers):
    """Bridged goes through LiteLLM, which only knows `model_name`."""
    c = providers["cline"]
    assert not c.native
    names = [n for n, _ in c.models]
    assert "deepseek-v4.1-flash" in names
    assert "deepseek-v4.1-flash[1m]" not in names  # marker is added on the wire
    assert "cline-deepseek-v4.1-flash" not in names  # prefix stripped


def test_native_folders_expose_upstream_ids(providers):
    """Native talks straight to the vendor, which wants its own spelling."""
    t = providers["trae"]
    assert t.native
    names = [n for n, _ in t.models]
    assert "DeepSeek-V4.1-Flash" in names  # vendor capitalisation is preserved
    # The selectable name IS the upstream id: a native endpoint never sees an
    # alias, so there is nothing to strip.
    assert dict(t.models)["DeepSeek-V4.1-Flash"] == "DeepSeek-V4.1-Flash"

    o = providers["opendesign"]
    assert o.native
    assert "deepseek-v4.1-flash" in [n for n, _ in o.models]


# ── resolution ───────────────────────────────────────────────────────────

def test_resolve_without_a_model_gives_the_profile_default(providers):
    assert providers["trae"].resolve("")[0] == "DeepSeek-V4.1-Flash"
    assert providers["cline"].resolve("")[0] == "deepseek-v4.1-flash"


def test_model_names_match_case_insensitively_but_keep_vendor_spelling():
    """Trae's registry says `DeepSeek-V4.1-Flash`; a user typing lowercase still
    gets that exact string back, because normalising it would 404 upstream."""
    rc, out, _ = run("resolve", "trae", "deepseek-v4.1-flash")
    assert rc == 0
    assert kv(out)["MODEL"] == "DeepSeek-V4.1-Flash"


def test_bridged_models_get_the_1m_marker_only_when_the_window_is_1m():
    """The marker follows the *window*, not the provider.

    It is a bridged-path convention: Claude Code sends `name[1m]` for a model
    whose window is 1M or more, and the watchdog strips the suffix before the
    request goes upstream. A model with a smaller window must not carry it —
    the name would match no deployment at all.
    """
    rc, out, _ = run("resolve", "cline", "deepseek-v4.1-flash")
    assert kv(out)["MODEL"].endswith("[1m]")  # 1048576 context — the marker applies

    # Under 1M the marker must not appear. Every shipped provider carries a
    # 1M-class window now, so the negative branch is pinned on a synthetic
    # registry row rather than on a model that has been retired.
    small_models = {
        "version": 1,
        "providers": [{
            "name": "demo",
            "api_base": "https://demo.invalid",
            "endpoint_kind": "openai",
            "auth_env": "DEMO_API_KEY",
            "models": [{"name": "demo-small", "ctx": 64000, "max_output": 8192,
                        "vision": False, "endpoint_kind": "openai"}],
        }],
    }
    small_profiles = {
        "version": 1,
        "profiles": {
            "demo": {"provider": "demo", "primary_tier": "default",
                     "tiers": {"default": {"model": "demo-small"}}},
        },
        "aliases": {
            "demo-demo-small": {"provider": "demo", "model": "demo-small", "ctx": 64000},
        },
    }
    p = ccp_pick.Provider("demo", small_profiles, small_models)
    name, upstream = p.resolve("demo-small")
    assert p.wire_name(name, upstream) == "demo-demo-small"  # no [1m] under 1M


def test_native_models_never_get_the_1m_marker():
    """Native endpoints never see the watchdog, so `[1m]` would be a literal
    model name there.

    Trae's window is 1M, so this is the case that matters: the marker looks
    like it applies and still must not be there.
    """
    rc, out, _ = run("resolve", "trae", "DeepSeek-V4.1-Flash")
    assert kv(out)["MODEL"] == "DeepSeek-V4.1-Flash"
    assert "[1m]" not in kv(out)["MODEL"]


def test_resolve_reports_the_real_context_window():
    """CLAUDE_CODE_MAX_CONTEXT_TOKENS is per-profile, so switching to a smaller
    model must not leave the old window advertised."""
    _, out, _ = run("resolve", "trae", "DeepSeek-V4.1-Flash")
    assert kv(out)["CTX"] == "1000000"  # the verified window
    _, out, _ = run("resolve", "cline", "deepseek-v4.1-flash")
    assert kv(out)["CTX"] == "1048576"  # a different window from the row above


def test_unknown_model_is_rejected_and_shows_the_options(providers):
    """The rejection must be actionable: the valid models ride along with it.

    bin/ccp merges the two streams (`2>&1`), so the user sees the error and the
    list together.
    """
    rc, out, err = run("resolve", "trae", "no-such-model")
    assert rc == 1
    assert "is not a trae model" in err
    assert "DeepSeek-V4.1-Flash" in out  # the listing comes with the error


def test_unknown_provider_is_rejected():
    rc, _, err = run("resolve", "nosuchprovider", "x")
    assert rc == 1
    assert "no provider" in err


def test_a_model_is_not_resolvable_from_the_wrong_folder(providers):
    """Guard against cross-provider slips: another provider's id does not
    belong to cline.

    Trae is no use as the example — its `DeepSeek-V4.1-Flash` and cline's
    selectable `deepseek-v4.1-flash` are the same model and match
    case-insensitively on purpose. The ids below belong to vendors whose
    models cline does not serve.
    """
    with pytest.raises(KeyError):
        providers["cline"].resolve("MiniMax-M3.1-Flash")
    with pytest.raises(KeyError):
        providers["cline"].resolve("deepseek-flash")


def test_folders_command_lists_one_line_per_provider():
    rc, out, _ = run("folders")
    assert rc == 0
    rows = [ln for ln in out.strip().splitlines() if ln.strip()]
    # One row per provider, whatever the registry currently ships. Pinning this
    # to a literal count made adding a provider — the normal way this list
    # grows — fail the suite for no behavioural reason.
    expected = _registry_provider_names()
    assert len(rows) == len(expected), f"{len(rows)} rows for {len(expected)} providers"
    for row in rows:
        name, kind, count, default, _ctx = row.split("|")
        assert name in expected
        assert kind in {"native", "bridged"}
        assert int(count) > 0
        assert default

# ── verification: the catalogue is a claim, the response is the evidence ──────
# MiniMax answers a request for `MiniMax-M3.1` with HTTP 200 and a completion
# from M3, and any unknown name falls back to M3 the same way. So "it returned
# 200" proves nothing; only the `model` field in the body does.


def test_same_model_tolerates_the_1m_marker_and_case():
    assert cp._same_model("cline-deepseek-v4.1-flash[1m]", "cline-deepseek-v4.1-flash")
    assert cp._same_model("DeepSeek-V4.1-Flash", "deepseek-v4.1-flash")
    assert cp._same_model("openai/deepseek-v4.1-flash", "deepseek-v4.1-flash")


def test_same_model_rejects_a_different_model():
    assert not cp._same_model("deepseek-v4.1-flash", "deepseek-v4.1-flash-preview")
    assert not cp._same_model("claude-haiku-5.5", "deepseek-flash")


def test_verify_cache_key_includes_the_credential(monkeypatch, tmp_path):
    """The same model name can mean different things under different keys.

    MiniMax's standard key once answered a request for the then-current
    M3.1-Preview with a completion from M3 while the plan key served it, and
    any account can be downgraded the same way. A cache keyed on provider+model
    alone would carry the old verdict across a key change and report a
    downgrade that stopped being true.
    """
    monkeypatch.setattr(cp, "_installed", lambda p: {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:1", "ANTHROPIC_AUTH_TOKEN": "key-one"})
    monkeypatch.setattr(cp, "VERIFY_CACHE", tmp_path / "v.json")
    cp.verify("minimax", "MiniMax-M3.1-Flash")  # fails to connect, but records a key
    stored = json.loads((tmp_path / "v.json").read_text())
    assert len(stored) == 1
    assert "key-one" not in next(iter(stored))  # the digest stands in for it

    monkeypatch.setattr(cp, "_installed", lambda p: {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:1", "ANTHROPIC_AUTH_TOKEN": "key-two"})
    cp.verify("minimax", "MiniMax-M3.1-Flash")
    assert len(json.loads((tmp_path / "v.json").read_text())) == 2


def test_verify_reports_no_profile_rather_than_claiming_a_downgrade(monkeypatch):
    monkeypatch.setattr(cp, "_installed", lambda p: {})
    served, note = cp.verify("minimax", "MiniMax-M3.1-Flash")
    assert served is None
    assert "no profile" in note


def test_verify_flags_the_200_with_nothing_in_it(monkeypatch):
    """Trae at 1.25M tokens answers 200 with an empty body and zero usage —
    a silent failure that looks exactly like success to a status-code check."""
    monkeypatch.setattr(cp, "_installed", lambda p: {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:1", "ANTHROPIC_AUTH_TOKEN": "k"})
    monkeypatch.setattr(cp, "VERIFY_CACHE", pathlib.Path(os.devnull))
    monkeypatch.setattr(cp.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(
        json.dumps({"model": "q", "content": [], "usage": {"output_tokens": 0}}).encode()))
    served, note = cp.verify("trae", "q")
    assert served == "q"
    assert "empty" in note
