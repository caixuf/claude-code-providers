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
    q = providers["qianfan"]
    assert not q.native
    names = [n for n, _ in q.models]
    assert "deepseek-v4.1-flash" in names
    assert "deepseek-v4.1-flash[1m]" not in names  # marker is added on the wire
    assert "qianfan-deepseek-v4.1-flash" not in names  # prefix stripped


def test_native_folders_expose_upstream_ids(providers):
    """Native talks straight to the vendor, which wants its own spelling."""
    t = providers["trae"]
    assert t.native
    names = [n for n, _ in t.models]
    assert "qwen3.8-max" in names
    assert "DeepSeek-V4.1-Flash" in names  # vendor capitalisation is preserved


# ── resolution ───────────────────────────────────────────────────────────

def test_resolve_without_a_model_gives_the_profile_default(providers):
    assert providers["trae"].resolve("")[0] == "qwen3.8-max"
    assert providers["qianfan"].resolve("")[0] == "deepseek-v4.1-flash"


def test_model_names_match_case_insensitively_but_keep_vendor_spelling():
    """Trae's registry says `DeepSeek-V4.1-Flash`; a user typing lowercase still
    gets that exact string back, because normalising it would 404 upstream."""
    rc, out, _ = run("resolve", "trae", "deepseek-v4.1-flash")
    assert rc == 0
    assert kv(out)["MODEL"] == "DeepSeek-V4.1-Flash"


def test_bridged_models_get_the_1m_marker_only_when_the_window_is_1m():
    rc, out, _ = run("resolve", "qianfan", "deepseek-v4.1-flash")
    assert kv(out)["MODEL"].endswith("[1m]")
    # 1.18M context — the marker still applies
    rc, out, _ = run("resolve", "qianfan", "glm-5.2")
    assert kv(out)["MODEL"].endswith("[1m]")


def test_native_models_never_get_the_1m_marker():
    """Native endpoints never see the watchdog, so `[1m]` would be a literal
    model name there."""
    rc, out, _ = run("resolve", "trae", "qwen3.8-max")
    assert kv(out)["MODEL"] == "qwen3.8-max"
    assert "[1m]" not in kv(out)["MODEL"]


def test_resolve_reports_the_real_context_window():
    """CLAUDE_CODE_MAX_CONTEXT_TOKENS is per-profile, so switching to a smaller
    model must not leave the old window advertised."""
    _, out, _ = run("resolve", "trae", "qwen3.8-max")
    assert kv(out)["CTX"] == "1000000"  # verified window, not Trae's advertised 96k-184k
    _, out, _ = run("resolve", "qianfan", "deepseek-v4.1-flash")
    assert kv(out)["CTX"] == "1441792"


def test_unknown_model_is_rejected_and_shows_the_options(providers):
    """The rejection must be actionable: the valid models ride along with it.

    bin/ccp merges the two streams (`2>&1`), so the user sees the error and the
    list together.
    """
    rc, out, err = run("resolve", "trae", "no-such-model")
    assert rc == 1
    assert "is not a trae model" in err
    assert "qwen3.8-max" in out  # the listing comes with the error


def test_unknown_provider_is_rejected():
    rc, _, err = run("resolve", "nosuchprovider", "x")
    assert rc == 1
    assert "no provider" in err


def test_a_model_is_not_resolvable_from_the_wrong_folder(providers):
    """Guard against cross-provider slips: trae's ids do not belong to cline."""
    with pytest.raises(KeyError):
        providers["cline"].resolve("qwen3.8-max")


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
# from M3, and echoes the name it was asked for in some paths. So "it returned
# 200" proves nothing; only the `model` field in the body does.


def test_same_model_tolerates_the_1m_marker_and_case():
    assert cp._same_model("qianfan-deepseek-v4.1-flash[1m]", "qianfan-deepseek-v4.1-flash")
    assert cp._same_model("MiniMax-M3", "minimax-M3")
    assert cp._same_model("openai/deepseek-v4.1-flash", "deepseek-v4.1-flash")


def test_same_model_rejects_a_different_model():
    assert not cp._same_model("MiniMax-M3", "MiniMax-M3.1-Flash-Preview")
    assert not cp._same_model("claude-haiku-5.5", "MiniMax-M3")


def test_verify_cache_key_includes_the_credential(monkeypatch, tmp_path):
    """The same model name means different things under different keys.

    MiniMax's standard key serves M3 for MiniMax-M3.1; the M Plan key serves
    M3.1. A cache keyed on provider+model alone would carry the old verdict
    across a key change and report a downgrade that stopped being true.
    """
    monkeypatch.setattr(cp, "_installed", lambda p: {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:1", "ANTHROPIC_AUTH_TOKEN": "key-one"})
    monkeypatch.setattr(cp, "VERIFY_CACHE", tmp_path / "v.json")
    cp.verify("minimax", "M")           # fails to connect, but records a key
    stored = json.loads((tmp_path / "v.json").read_text())
    assert len(stored) == 1
    assert "key-one" not in next(iter(stored))  # the digest stands in for it

    monkeypatch.setattr(cp, "_installed", lambda p: {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:1", "ANTHROPIC_AUTH_TOKEN": "key-two"})
    cp.verify("minimax", "M")
    assert len(json.loads((tmp_path / "v.json").read_text())) == 2


def test_verify_reports_no_profile_rather_than_claiming_a_downgrade(monkeypatch):
    monkeypatch.setattr(cp, "_installed", lambda p: {})
    served, note = cp.verify("minimax", "MiniMax-M3")
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
