#!/usr/bin/env python3
"""Show the routing chain and the quota evidence behind it.

A quota-aware router needs a quota to read. omp's broker publishes per-account
plan windows; OpenDesign, MiniMax and Qianfan do not publish one in this
installation. This command therefore does not pretend to rank providers by
"remaining quota" it cannot see. It shows:

  * the exact member -> fallback order of each group;
  * which of those deployments is the omp gateway;
  * omp's current window usage, or the reason it cannot be read.

`--strict` is for a timer: it exits 1 when omp is in a group but is either
unreachable or below the remaining-quota threshold, which is the point where
the fallback should be treated as dead rather than tried.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent
REPO = BRIDGE.parent
sys.path.insert(0, str(BRIDGE))

import ccp_quota  # noqa: E402
import render_config  # noqa: E402

PROFILES = REPO / "registry" / "profiles.yaml"
MODELS = REPO / "registry" / "models.yaml"


def load() -> tuple[dict, dict]:
    return (
        render_config.load_yaml(PROFILES),
        render_config.load_yaml(MODELS),
    )


def omp_health() -> dict:
    try:
        report = ccp_quota.read_omp()
    except Exception as exc:
        return {"known": False, "error": str(exc), "windows": [], "remaining": None}
    windows = report.get("windows") or []
    fractions = []
    for window in windows:
        total = window.get("total") or 0
        used = window.get("used") or 0
        if total:
            fractions.append(max(0.0, min(1.0, (total - used) / total)))
    return {
        "known": True,
        "error": None,
        "windows": windows,
        "remaining": min(fractions) if fractions else None,
    }


def member_rows(profiles: dict, models: dict, group: str) -> list[dict]:
    spec = (profiles.get("groups") or {}).get(group) or {}
    rows = []
    for phase, key in (("member", "members"), ("fallback", "fallback")):
        for member in spec.get(key) or []:
            provider, model = render_config._split_member(profiles, models, member)
            rows.append({
                "phase": phase,
                "name": member,
                "provider": provider or "?",
                "model": model,
                "omp": provider == "omp",
            })
    return rows


def plan(profiles: dict, models: dict) -> dict:
    health = omp_health()
    groups = {}
    for group in (profiles.get("groups") or {}):
        groups[group] = {
            "chain": member_rows(profiles, models, group),
            "omp_present": any(row["omp"] for row in member_rows(profiles, models, group)),
        }
    return {"omp": health, "groups": groups}


def render_plan(data: dict, threshold: float) -> None:
    health = data["omp"]
    if health["known"]:
        remaining = health.get("remaining")
        text = "unknown"
        if remaining is not None:
            text = f"{remaining * 100:.1f}% left (lowest window)"
        print(f"\033[36momp quota\033[0m  {text}")
        for window in health.get("windows") or []:
            total = window.get("total") or 0
            used = window.get("used") or 0
            left = max(0.0, total - used)
            print(f"  {window['name'][:44]:44s} {left:6.1f} / {total:.0f} {window.get('unit','')}")
    else:
        print(f"\033[33momp quota\033[0m  unavailable: {health.get('error')}")

    for group, info in data["groups"].items():
        print(f"\n\033[36mgroup {group}\033[0m")
        for row in info["chain"]:
            mark = "\033[33momp\033[0m" if row["omp"] else row["provider"]
            print(f"  {row['phase']:<8} {row['name']:<38} {mark}")
        if info["omp_present"] and health["known"]:
            remaining = health.get("remaining")
            if remaining is not None and remaining < threshold:
                print(f"  \033[31m! omp is below the {threshold:.0%} threshold; the fallback is effectively dead\033[0m")
        elif info["omp_present"]:
            print("  \033[31m! omp is in the chain but its quota is unreadable\033[0m")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ccp route", description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 when an omp member is unreadable or below threshold")
    ap.add_argument("--threshold", type=float, default=0.10,
                    help="remaining fraction considered dead (default: 0.10)")
    args = ap.parse_args(argv)

    profiles, models_doc = load()
    models, _ = render_config.index_models(models_doc)
    data = plan(profiles, models)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        render_plan(data, args.threshold)

    if args.strict:
        health = data["omp"]
        present = any(info["omp_present"] for info in data["groups"].values())
        if present and not health["known"]:
            return 1
        remaining = health.get("remaining")
        if present and remaining is not None and remaining < args.threshold:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
