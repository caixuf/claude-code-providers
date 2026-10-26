#!/usr/bin/env python3
"""Which local profiles still point at a model the bridge serves?

A provider can be retired while a profile for it stays installed on the
machine. Nothing complains: the profile is a valid file, the bridge is up,
and the request only fails when somebody selects it — with a 400 that reads
like the bridge is down.

So the check is derived, never a hand-written list. The served set is what
two sources say, and they cover different halves of it:

* the repository's rendered templates (``providers/*.json.example``) name the
  models the **native** providers answer to (deepseek-flash, gpt-6.1-sol,
  MiniMax-M3.1-Flash, ...), which never reach LiteLLM and therefore never
  appear in the proxy's own model list;
* the running bridge's ``/v1/models`` names every **bridged** deployment,
  including the ``[1m]`` spellings Claude Code asks for.

A profile is dead when none of the models it names is in that union.

Usage:
  profile_audit.py [--providers-dir DIR] [--repo DIR] [--bridge URL] [--json]
  profile_audit.py --dead          # only the dead profiles, one file name per line
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent
sys.path.insert(0, str(BRIDGE))

# The env keys a profile uses to name models. `model` is the top-level
# shorthand Claude Code also reads.
MODEL_KEYS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
)

DEFAULT_BRIDGE = "http://127.0.0.1:4000"
MASTER_KEY = "sk-ccp-litellm-local"


def profile_models(path: Path) -> list[str]:
    """Every model name a profile asks for, in the order it names them."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    env = doc.get("env") or {}
    out = [env[k] for k in MODEL_KEYS if isinstance(env.get(k), str) and env.get(k)]
    model = doc.get("model")
    if isinstance(model, str) and model:
        out.append(model)
    seen: list[str] = []
    for m in out:
        if m not in seen:
            seen.append(m)
    return seen


def served_names(repo: Path, bridge: str | None = DEFAULT_BRIDGE) -> set[str]:
    """Every model name the bridge answers to, from templates and the bridge."""
    names: set[str] = set()
    for ex in sorted((repo / "providers").glob("*.json.example")):
        names.update(profile_models(ex))

    if bridge:
        try:
            req = urllib.request.Request(
                bridge.rstrip("/") + "/v1/models",
                headers={"Authorization": "Bearer " + MASTER_KEY},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                doc = json.loads(resp.read().decode("utf-8"))
            names.update(m["id"] for m in doc.get("data", []) if m.get("id"))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            # The bridge being down is not the audit's business: the templates
            # still name every native model, and a bridged profile whose name
            # only the bridge knows is caught the moment the bridge is up.
            pass
    return names


def audit(providers_dir: Path, repo: Path, bridge: str | None = DEFAULT_BRIDGE) -> list[dict]:
    """One row per installed profile: what it names, and what is missing."""
    served = served_names(repo, bridge)
    rows: list[dict] = []
    for path in sorted(providers_dir.glob("*.json")):
        models = profile_models(path)
        if not models:
            continue
        missing = [m for m in models if m not in served]
        rows.append(
            {
                "profile": path.name,
                "models": models,
                "missing": missing,
                "dead": bool(missing),
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--providers-dir", default=str(Path.home() / ".claude" / "providers"))
    ap.add_argument("--repo", default=str(BRIDGE.parent))
    ap.add_argument("--bridge", default=DEFAULT_BRIDGE,
                    help="bridge base URL (empty string to skip the live check)")
    ap.add_argument("--dead", action="store_true", help="only the dead profiles")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    rows = audit(
        Path(args.providers_dir).expanduser(),
        Path(args.repo).expanduser(),
        args.bridge or None,
    )

    if args.dead:
        for row in rows:
            if row["dead"]:
                print(row["profile"])
        return 0

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    if not rows:
        print("no installed profiles")
        return 0
    width = max(len(r["profile"]) for r in rows)
    for row in rows:
        mark = "\033[31mdead\033[0m" if row["dead"] else "\033[32m ok \033[0m"
        detail = ", ".join(row["missing"]) if row["dead"] else ", ".join(row["models"])
        print(f"  {row['profile']:<{width}}  {mark}  {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
