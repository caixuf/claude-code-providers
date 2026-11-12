"""The route view must show the chain it will actually use, and must not
invent quota for providers that do not publish one."""

from __future__ import annotations

import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bridge"))

import ccp_route  # noqa: E402


def _docs():
    profiles = {
        "groups": {
            "main": {
                "members": ["provider-a"],
                "fallback": ["MiniMax-M3.1-Flash", "omp-fast"],
            }
        },
        "aliases": {
            "omp-fast": {"provider": "omp", "model": "opencode-go/deepseek-v4.1-flash"},
        },
        "providers": {"omp": {"auth_env": "OMP_AUTH_GATEWAY_TOKEN"}},
    }
    models = {
        ("provider-a", "model-a"): {},
        ("minimax", "MiniMax-M3.1-Flash"): {},
        ("omp", "opencode-go/deepseek-v4.1-flash"): {},
    }
    return profiles, models


def test_member_rows_keep_the_curated_order_and_mark_omp():
    profiles, models = _docs()
    rows = ccp_route.member_rows(profiles, models, "main")
    assert [r["name"] for r in rows] == [
        "provider-a", "MiniMax-M3.1-Flash", "omp-fast"
    ]
    assert rows[0]["phase"] == "member"
    assert rows[1]["phase"] == "fallback"
    assert [r["omp"] for r in rows] == [False, False, True]


def test_plan_marks_quota_unknown_without_pretending(monkeypatch):
    profiles, models = _docs()
    monkeypatch.setattr(
        ccp_route,
        "omp_health",
        lambda: {"known": False, "error": "omp not installed", "windows": [], "remaining": None},
    )
    data = ccp_route.plan(profiles, models)
    assert data["omp"]["known"] is False
    assert data["groups"]["main"]["omp_present"] is True


def test_plan_marks_quota_known(monkeypatch):
    profiles, models = _docs()
    monkeypatch.setattr(
        ccp_route,
        "omp_health",
        lambda: {"known": True, "error": None, "windows": [], "remaining": 0.96},
    )
    data = ccp_route.plan(profiles, models)
    assert data["omp"]["remaining"] == 0.96
