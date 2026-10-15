"""Tests for bridge/ccp_pick.py — the provider-as-folder resolution.

The distinction under test is the one that silently breaks model switching: a
*native* provider is addressed by its upstream id, while a *bridged* one is
addressed by the alias that LiteLLM actually accepts. Getting it wrong writes a
model name the endpoint rejects, and the session only fails later.
"""

import pathlib
import subprocess
import sys

import pytest
import yaml

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
    assert kv(out)["CTX"] == "168000"
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
    assert len(rows) == 6
    for row in rows:
        name, kind, count, default, _ctx = row.split("|")
        assert kind in {"native", "bridged"}
        assert int(count) > 0
        assert default