"""The audit that decides which installed profiles are dead.

A provider retired yesterday leaves a profile behind on every machine that
had it installed: a valid file, a live bridge, and a 400 that reads like the
bridge is down — but only when somebody selects it. The check is derived, so
these tests pin the derivation rather than a list.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))

import profile_audit  # noqa: E402


def _profile(path: Path, model: str) -> Path:
    path.write_text(json.dumps({
        "env": {
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
            "ANTHROPIC_MODEL": model,
        },
        "model": model,
    }))
    return path


def _repo(tmp_path: Path, *templates: tuple[str, str]) -> Path:
    """A repository whose rendered templates name the given models."""
    repo = tmp_path / "repo"
    (repo / "providers").mkdir(parents=True)
    for name, model in templates:
        _profile(repo / "providers" / f"{name}.json.example", model)
    return repo


def test_a_profile_naming_a_served_model_is_alive(tmp_path):
    repo = _repo(tmp_path, ("qianfan", "qianfan-deepseek-v4.1-flash[1m]"))
    profiles = tmp_path / "providers"
    profiles.mkdir()
    _profile(profiles / "qianfan.json", "qianfan-deepseek-v4.1-flash[1m]")
    rows = profile_audit.audit(profiles, repo, bridge="")
    assert [r["dead"] for r in rows] == [False], rows


def test_a_profile_naming_a_retired_model_is_dead(tmp_path):
    repo = _repo(tmp_path, ("qianfan", "qianfan-deepseek-v4.1-flash[1m]"))
    profiles = tmp_path / "providers"
    profiles.mkdir()
    _profile(profiles / "workbuddy.json", "workbuddy-deepseek-v4.1-flash[1m]")
    rows = profile_audit.audit(profiles, repo, bridge="")
    assert rows[0]["dead"] is True
    assert rows[0]["missing"] == ["workbuddy-deepseek-v4.1-flash[1m]"]


def test_the_bridge_is_asked_when_it_is_up(tmp_path):
    """A bridged profile's name is only the bridge's to answer for."""
    repo = _repo(tmp_path)
    profiles = tmp_path / "providers"
    profiles.mkdir()
    _profile(profiles / "cline.json", "cline-deepseek-v4.1-flash")
    # no template names it, so only the live bridge can clear it
    rows = profile_audit.audit(profiles, repo, bridge="")
    assert rows[0]["dead"] is True


def test_every_model_key_of_a_profile_is_checked(tmp_path):
    repo = _repo(tmp_path, ("qianfan", "qianfan-deepseek-v4.1-flash"))
    profiles = tmp_path / "providers"
    profiles.mkdir()
    (profiles / "mixed.json").write_text(json.dumps({
        "env": {
            "ANTHROPIC_MODEL": "qianfan-deepseek-v4.1-flash",
            "ANTHROPIC_SMALL_FAST_MODEL": "qianfan-glm-5.3-flash",
        },
    }))
    rows = profile_audit.audit(profiles, repo, bridge="")
    assert rows[0]["missing"] == ["qianfan-glm-5.3-flash"], rows


def test_an_unreadable_profile_is_not_a_dead_one(tmp_path):
    """A broken file is a different problem and must not be archived."""
    repo = _repo(tmp_path, ("qianfan", "qianfan-deepseek-v4.1-flash"))
    profiles = tmp_path / "providers"
    profiles.mkdir()
    (profiles / "broken.json").write_text("{not json")
    rows = profile_audit.audit(profiles, repo, bridge="")
    assert rows == [], rows
