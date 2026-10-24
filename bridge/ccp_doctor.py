#!/usr/bin/env python3
"""Is this machine still consistent with the repository?

Three things drift silently, and each one fails late and confusingly:

* a **profile** left installed for a provider that was retired — valid file,
  bridge up, and the request only fails when somebody selects it, with a 400
  that reads like the bridge is down;
* the **active** profile naming a model the bridge no longer serves;
* the **Hermes bot** left on a model that was retired, which fails only when
  somebody messages it.

The served set is derived (see profile_audit.py), so this needs no list to
maintain: it asks the repository's templates and the running bridge what they
serve, and compares.

Usage:
  ccp_doctor.py [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent
sys.path.insert(0, str(BRIDGE))

import profile_audit  # noqa: E402

MODEL_KEYS = profile_audit.MODEL_KEYS


def hermes_home() -> Path:
    """Read at call time: the home is an environment override, not a constant."""
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def _hermes_configs() -> list[Path]:
    """The Hermes config: one file for a single-config install, one per profile
    otherwise. The bot is whatever profile is active, so the active one is the
    one that matters."""
    profiles = hermes_home() / "profiles"
    if profiles.is_dir():
        return sorted(profiles.glob("*/config.yaml"))
    single = hermes_home() / "config.yaml"
    return [single] if single.is_file() else []


def hermes_models(path: Path) -> list[str]:
    """The model names a Hermes config asks for, read without a YAML parser.

    The file is hand-written and heavily commented; a parser would need the
    whole document to be valid to answer one question, and a Hermes config
    that is halfway edited must still be reportable.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for key in ("default_model:", "default:"):
            if stripped.startswith(key):
                value = stripped[len(key):].split("#")[0].strip().strip("'\"")
                if value and value not in out:
                    out.append(value)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--providers-dir", default=str(Path.home() / ".claude" / "providers"))
    ap.add_argument("--settings", default=str(Path.home() / ".claude" / "settings.json"))
    ap.add_argument("--repo", default=str(BRIDGE.parent))
    ap.add_argument("--bridge", default=profile_audit.DEFAULT_BRIDGE)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    repo = Path(args.repo).expanduser()
    served = profile_audit.served_names(repo, args.bridge or None)
    problems: list[str] = []

    profiles = profile_audit.audit(
        Path(args.providers_dir).expanduser(), repo, args.bridge or None
    )
    dead = [row for row in profiles if row["dead"]]
    for row in dead:
        problems.append(
            f"profile {row['profile']} names {', '.join(row['missing'])}, "
            "which the bridge no longer serves"
        )

    # The active profile: what Claude Code will actually ask for.
    active_model = None
    try:
        settings = json.loads(Path(args.settings).expanduser().read_text(encoding="utf-8"))
        active_model = (settings.get("env") or {}).get("ANTHROPIC_MODEL")
    except (OSError, ValueError):
        pass
    if active_model and active_model not in served:
        problems.append(f"the active profile asks for {active_model}, which the bridge no longer serves")

    bots = []
    for cfg in _hermes_configs():
        models = hermes_models(cfg)
        missing = [m for m in models if m not in served]
        bots.append({"config": str(cfg), "models": models, "missing": missing})
        for m in missing:
            problems.append(f"the Hermes bot at {cfg} asks for {m}, which the bridge no longer serves")

    if args.json:
        print(json.dumps(
            {
                "served": len(served),
                "profiles": profiles,
                "active_model": active_model,
                "bots": bots,
                "problems": problems,
            },
            indent=2,
        ))
        return 1 if problems else 0

    print(f"the bridge serves {len(served)} model names")
    print(f"installed profiles: {len(profiles)} ({len(dead)} dead)")
    print(f"active profile model: {active_model or '(unset)'}")
    for bot in bots:
        state = "ok" if not bot["missing"] else f"dead: {', '.join(bot['missing'])}"
        print(f"hermes bot: {bot['config']} -> {', '.join(bot['models']) or '(none)'} [{state}]")
    if problems:
        print()
        for p in problems:
            print(f"  ✗ {p}")
        print("\n`ccp sync` archives the dead profiles; `ccp <provider>` repoints the bot.")
        return 1
    print("\n  ✔ everything installed still points at a model the bridge serves")
    return 0


if __name__ == "__main__":
    sys.exit(main())
