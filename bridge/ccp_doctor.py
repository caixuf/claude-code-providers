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
import re
import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent
sys.path.insert(0, str(BRIDGE))

import bridge_auth  # noqa: E402
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


def hermes_credentials(path: Path) -> list[dict]:
    """The (base_url, api_key) pairs in a hand-written Hermes config.

    Read line by line, the way hermes_models() reads the model names: the
    file is heavily commented and a parser would need the whole document to
    be valid to answer one question.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict] = []
    base: tuple[str, int] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if base is not None and indent < base[1]:
            base = None
        m = re.match(r"base_url\s*:\s*[\"\']?([^\"\'\s]+)", line)
        if m:
            base = (m.group(1), indent)
            continue
        m = re.match(r"api_key\s*:\s*[\"\']?([^\"\'\s]+)", line)
        if m and base is not None:
            out.append({"base_url": base[0], "api_key": m.group(1)})
            base = None
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
    warnings: list[str] = []

    # The credential the bridge actually accepts: the master key in the
    # config LiteLLM runs on. A client that carries another one still gets
    # through - the bridge forwards this one - so a mismatch is a stale
    # config, not a broken one.
    master_key, key_source = bridge_auth.load_master_key()

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
    active_base = None
    active_token = None
    try:
        settings = json.loads(Path(args.settings).expanduser().read_text(encoding="utf-8"))
        env = settings.get("env") or {}
        active_model = env.get("ANTHROPIC_MODEL")
        active_base = env.get("ANTHROPIC_BASE_URL")
        active_token = env.get("ANTHROPIC_AUTH_TOKEN")
    except (OSError, ValueError):
        pass
    if active_model and active_model not in served:
        problems.append(f"the active profile asks for {active_model}, which the bridge no longer serves")
    if bridge_auth.is_bridge_url(active_base) and active_token != master_key:
        warnings.append(
            "the active profile points at the bridge with a credential that is not the "
            f"bridge's key (it will be replaced, so calls still work; `ccp sync` repairs it)"
        )

    bots = []
    for cfg in _hermes_configs():
        models = hermes_models(cfg)
        missing = [m for m in models if m not in served]
        creds = hermes_credentials(cfg)
        bots.append({"config": str(cfg), "models": models, "missing": missing, "credentials": creds})
        for m in missing:
            problems.append(f"the Hermes bot at {cfg} asks for {m}, which the bridge no longer serves")
        for cred in creds:
            if bridge_auth.is_bridge_url(cred["base_url"]) and cred["api_key"] != master_key:
                warnings.append(
                    f"the Hermes bot at {cfg} points at the bridge with a credential that is "
                    "not the bridge's key (it will be replaced, so calls still work)"
                )

    if args.json:
        print(json.dumps(
            {
                "served": len(served),
                "profiles": profiles,
                "active_model": active_model,
                "bots": bots,
                "problems": problems,
                "warnings": warnings,
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
    if warnings:
        print()
        for w in warnings:
            print(f"  \u26a0 {w}")
    if problems:
        print()
        for p in problems:
            print(f"  \u2717 {p}")
        print("\n`ccp sync` archives the dead profiles; `ccp <provider>` repoints the bot.")
        return 1
    print("\n  \u2714 everything installed still points at a model the bridge serves")
    return 0


if __name__ == "__main__":
    sys.exit(main())
