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
