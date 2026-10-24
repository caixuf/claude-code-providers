#!/usr/bin/env python3
"""Point the Hermes agent at a model the ccp bridge actually serves.

Every provider ccp knows is reachable through the one local LiteLLM bridge on
:4000, so switching provider changes exactly one thing for Hermes: the model
name it asks for. That name has to be one the proxy emitted — the curated alias
when the registry has one, else the bare registry id (``render_config.
litellm_key_for``), which is the same rule `ccp render` used to write
``~/.config/litellm/config.yaml``. Guessing it produces a name no deployment
answers, and the resulting 400 reads like the bridge is down.

The profile file is hand-written and heavily commented — compression
thresholds, tool-loop guardrails, MCP servers, platform switches — so this
rewrites the two lines that carry the model name rather than re-emitting the
YAML. A round trip through a parser drops every comment in the file, which
nobody notices until the next person has to read it.

Usage:
  hermes_config.py render <provider> [model] [--profile <name>] [--dry-run]
  hermes_config.py status [--profile <name>]

`--profile` defaults to $HERMES_PROFILE, else to `~/.hermes/active_profile` —
the profile the bot is actually running on this host.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent
sys.path.insert(0, str(BRIDGE))
import render_config  # noqa: E402

# The ccp entry point inside a Hermes profile. Hermes calls each provider by its
# configured name, and `custom:` is how it spells "not built in".
PROVIDER_NAME = "ccp"
PROVIDER_REF = f"custom:{PROVIDER_NAME}"

# A key line and the indentation that says how deep it sits. A block header
# (`model:`) has no value, so the gap must be optional — requiring one silently
# skips every block the keys live in. Anchored at the line start on purpose: a
# list bullet or a value that happens to contain a colon must not match.
_KEY = re.compile(r"^(?P<indent> *)(?P<key>[A-Za-z_][\w-]*):(?P<gap>[ \t]*)(?P<rest>.*)$")

# Where the model name lives, per top-level block, and the name the result is
# reported under. The two differ for `providers`: the block is `providers`, but
# the key carrying the model is `default_model`.
_NAME_KEY = {"model": "default", "providers": "default_model"}
_SEEN_KEY = {"model": "model", "providers": "default_model"}


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def default_profile() -> str | None:
    """The profile the bot is running right now — read from Hermes, not guessed.

    Naming one host's profile in the shared repo would be wrong everywhere else:
    the bot on a workstation is `caixuf`, on the server it is `imfl`. The
    installed `hermes-gateway-<name>.service` names a profile, but it is not the
    one being asked about: several can be installed while `active_profile` holds
    the single file that says which is live.
    """
    env = os.environ.get("HERMES_PROFILE")
    if env:
        return env
    try:
        active = (hermes_home() / "active_profile").read_text(encoding="utf-8").strip()
    except OSError:
        active = ""
    return active or None


def profile_config(profile: str) -> Path:
    return hermes_home() / "profiles" / profile / "config.yaml"


def _require_profile(args) -> Path:
    """The config to act on, or a message saying how to name one."""
    if args.config:
        return Path(args.config)
    if not args.profile:
        found = sorted(p.name for p in (hermes_home() / "profiles").iterdir() if p.is_dir())
        raise SystemExit(
            "cannot tell which Hermes profile to edit: no gateway service installed "
            "(or more than one).\n"
            f"  pass --profile <name>; profiles here: {', '.join(found) or 'none'}"
        )
    return profile_config(args.profile)


def _split_comment(rest: str) -> tuple[str, str]:
    """Split a YAML value from a trailing comment, leaving both verbatim."""
    idx = rest.find(" #")
    if idx < 0:
        return rest, ""
    return rest[:idx].rstrip(), rest[idx:]


def _requote(old: str, new: str) -> str:
    """Keep the quoting style the file already uses."""
    for q in ('"', "'"):
        if len(old) >= 2 and old.startswith(q) and old.endswith(q):
            return f"{q}{new}{q}"
    return new


def rewrite(text: str, name: str | None = None) -> tuple[str, dict]:
    """Set the model name in `model.default` and `providers.<ccp>.default_model`.

    With ``name=None`` nothing is changed and the current values are only read.

    Returns the new text plus what was found, so the caller can refuse rather
    than write a half-updated file: a profile whose `model.provider` is not ccp
    is not ours to point at a ccp model name.
    """
    lines = text.splitlines(keepends=True)
    top = sub = None
    sub_indent = None
    seen: dict = {"model": None, "provider": None, "default_model": None}

    for i, raw in enumerate(lines):
        has_nl = raw.endswith("\n")
        body = raw[:-1] if has_nl else raw
        m = _KEY.match(body)
        if not m:
            continue
        indent, key = m.group("indent"), m.group("key")
        depth = len(indent)

        if depth == 0:
            top, sub, sub_indent = key, None, None
            continue

        # Inside `providers:`, the first deeper key names a provider, and every
        # key at that same depth names the next one.
        if top == "providers":
            if sub_indent is None or depth <= sub_indent:
                sub, sub_indent = key, depth
                continue

        if top == "model" and key == "provider":
            rest = m.group("rest").strip()
            seen["provider"] = rest.split()[0].strip("'\"") if rest else None
            continue

        if key != _NAME_KEY.get(top):
            continue
        if top == "providers" and sub != PROVIDER_NAME:
            continue

        value, comment = _split_comment(m.group("rest"))
        seen[_SEEN_KEY[top]] = value.strip().strip("'\"")
        if name is not None:
            new = _requote(value.strip(), name)
            tail = "\n" if has_nl else ""
            lines[i] = f"{indent}{key}:{m.group('gap')}{new}{comment}{tail}"

    return "".join(lines), seen


def resolve(provider: str, model: str | None, repo: Path | None = None) -> dict:
    root = repo or BRIDGE.parent
    models_doc = render_config.load_yaml(root / "registry" / "models.yaml")
    profiles_doc = render_config.load_yaml(root / "registry" / "profiles.yaml")
    models, _providers = render_config.index_models(models_doc)

    specs = profiles_doc.get("profiles") or {}
    if provider not in specs:
        known = ", ".join(sorted(specs))
        raise SystemExit(f"unknown provider '{provider}' (known: {known})")
    spec = specs[provider]
    pname = spec["provider"]

    if not model:
        primary = spec.get("primary_tier") or "default"
        model = (spec.get("tiers") or {})[primary]["model"]

    # Model names are matched case-insensitively, like the rest of ccp, but the
    # registry's spelling is what gets written.
    hit = next((m for (p, m) in models if p == pname and m.lower() == model.lower()), None)
    if hit is None:
        available = ", ".join(sorted(m for (p, m) in models if p == pname))
        raise SystemExit(f"'{model}' is not in the {pname} catalogue\n  available: {available}")

    meta = models[(pname, hit)]
    return {
        "provider": provider,
        "registry_model": hit,
        "name": render_config.litellm_key_for(profiles_doc, pname, hit),
        "ctx": meta.get("ctx"),
        "upstream": render_config.litellm_model_string(pname, hit, meta),
    }


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _profile_of(args, path: Path) -> str:
    """The name to restart a service for — empty when the file was named directly."""
    if args.profile:
        return args.profile
    parts = path.parts
    if "profiles" in parts and path.name == "config.yaml":
        return parts[parts.index("profiles") + 1]
    return ""


def cmd_render(args) -> int:
    path = _require_profile(args)
    if not path.is_file():
        raise SystemExit(f"no Hermes profile at {path}")

    info = resolve(args.provider, args.model, Path(args.repo) if args.repo else None)
    original = path.read_text(encoding="utf-8")
    updated, seen = rewrite(original, info["name"])

    if seen["provider"] is None:
        raise SystemExit(f"{path} has no `model: provider:` line — is this a Hermes profile?")
    if seen["provider"] != PROVIDER_REF:
        raise SystemExit(
            f"{path} runs model.provider: {seen['provider']}, not {PROVIDER_REF}.\n"
            "  ccp only manages the ccp entry point; point the profile at it first, "
            "or pass --config for the file you mean."
        )
    if seen["default_model"] is None:
        raise SystemExit(
            f"{path} has no `providers.{PROVIDER_NAME}.default_model` line. Add the block:\n\n"
            f"  providers:\n"
            f"    {PROVIDER_NAME}:\n"
            f"      name: {PROVIDER_NAME}\n"
            f'      base_url: "http://127.0.0.1:4000/v1"\n'
            f"      api_mode: chat_completions\n"
            f'      api_key: "<the LiteLLM master key>"\n'
            f"      default_model: {info['name']}\n"
        )

    changed = updated != original
    if args.json:
        print(json.dumps({**info, "file": str(path), "changed": changed}, indent=2))
        return 0

    print(f"profile:  {_profile_of(args, path) or '(named directly)'}  ({path})")
    print(f"provider: {info['provider']} -> {info['registry_model']}")
    print(f"model:    {seen['model']} -> {info['name']}   (proxy id; ctx {info['ctx']})")

    if not changed:
        print("already up to date.")
        print("CHANGED=0")
        return 0
    if args.dry_run:
        print("(dry run — nothing written)")
        print("CHANGED=0")
        return 0

    if not args.no_backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, path.with_name(path.name + f".bak.{stamp}"))
    _write_atomic(path, updated)
    print("written.")
    # The gateway is a live bot and caches its agent at startup, so a rewritten
    # config is inert until it is bounced. The caller needs the profile to name
    # the unit; `CHANGED=1` is the only signal that a bounce is owed.
    print(f"PROFILE={_profile_of(args, path)}")
    print("CHANGED=1")
    return 0


def cmd_status(args) -> int:
    path = _require_profile(args)
    if not path.is_file():
        raise SystemExit(f"no Hermes profile at {path}")
    _, seen = rewrite(path.read_text(encoding="utf-8"))
    print(json.dumps(
        {
            "profile": _profile_of(args, path),
            "file": str(path),
            "provider": seen["provider"],
            "model": seen["model"],
            "provider_default_model": seen["default_model"],
        },
        indent=2,
    ))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        # No hard-coded profile name: the bot is `caixuf` on a workstation and
        # `imfl` on the server, and this file ships to both.
        p.add_argument("--profile", default=default_profile(),
                       help="Hermes profile to edit (default: $HERMES_PROFILE, else "
                            "~/.hermes/active_profile — the one the bot is running)")
        p.add_argument("--config", help="explicit config.yaml (bypasses --profile)")
        p.add_argument("--repo", help="checkout holding registry/ (default: this repo)")

    r = sub.add_parser("render", help="write a provider's model into the profile")
    r.add_argument("provider")
    r.add_argument("model", nargs="?")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--no-backup", action="store_true")
    r.add_argument("--json", action="store_true")
    common(r)
    r.set_defaults(func=cmd_render)

    s = sub.add_parser("status", help="show what the profile currently runs")
    common(s)
    s.set_defaults(func=cmd_status)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())