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


def _serves_a_platform(profile: str) -> bool:
    """Whether a profile actually runs a messaging bot.

    A gateway with every platform switched off serves cron and the local API
    only. One host can have both installed — the bot and such a helper — and the
    bot is the one with a platform on. Never fatal: a profile that cannot be read
    simply is not a candidate, and the caller falls back to asking.
    """
    try:
        import yaml  # type: ignore
        doc = yaml.safe_load(profile_config(profile).read_text(encoding="utf-8")) or {}
    except Exception:
        return False
    return any(isinstance(v, dict) and v.get("enabled")
               for v in (doc.get("platforms") or {}).values())


def default_profile() -> str | None:
    """The profile of the bot this host runs.

    Naming one host's profile in the shared repo would be wrong everywhere else:
    the bot on a workstation is `caixuf`, on the server it is `imfl`.

    Deliberately **not** ``~/.hermes/active_profile``. That file is the sticky
    default a *bare* `hermes` command follows (``hermes_constants.py``), and it
    can name a profile with no gateway at all — on this workstation it said
    `ccp` while the running bot was `caixuf`, so `ccp hermes codex` would have
    rewritten `profiles/ccp/config.yaml` (which passes every check this command
    makes) and bounced a `hermes-gateway-ccp.service` that did not exist, leaving
    the real bot untouched with no error anywhere.

    An installed gateway is not enough on its own either, since several can be
    installed; only the one serving a platform is a bot. Anything still
    ambiguous falls through to `--profile` rather than to a guess — a guess here
    rewrites a live bot's config and restarts it.
    """
    env = os.environ.get("HERMES_PROFILE")
    if env:
        return env
    units = (Path.home() / ".config/systemd/user").glob("hermes-gateway-*.service")
    names = sorted({u.name[len("hermes-gateway-"):-len(".service")] for u in units})
    if len(names) == 1:
        return names[0]
    bots = [n for n in names if _serves_a_platform(n)]
    return bots[0] if len(bots) == 1 else None


def profile_config(profile: str) -> Path:
    return hermes_home() / "profiles" / profile / "config.yaml"


def _require_profile(args) -> Path:
    """The config to act on, or a message saying how to name one."""
    if args.config:
        return Path(args.config)
    if not args.profile:
        found = sorted(p.name for p in (hermes_home() / "profiles").iterdir() if p.is_dir())
        raise SystemExit(
            "cannot tell which Hermes profile to edit: $HERMES_PROFILE is unset and no "
            "single hermes-gateway-*.service names one.\n"
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


def rewrite(text: str, name: str | None = None, provider: str | None = None) -> tuple[str, dict]:
    """Set the model name in `model.default` and `providers.<ccp>.default_model`.

    With ``name=None`` nothing is changed and the current values are only read.

    ``provider`` also rewrites `model.provider`, and a migration needs it: that
    key is what picks the entry point, so a profile left on `custom:commandcode`
    keeps talking to commandcode however carefully the model name under
    `providers.ccp` is written. Rewriting the model alone would look like a
    switch and behave like no-op.

    Returns the new text plus what was found, so the caller can refuse rather
    than write a half-updated file.
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
            word = rest.split()[0] if rest else ""
            seen["provider"] = word.strip("'\"") if word else None
            if provider is not None and word:
                tail = "\n" if has_nl else ""
                lines[i] = f"{indent}{key}:{m.group('gap')}{_requote(word, provider)}{tail}"
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


def _ccp_block(name: str) -> str:
    return (
        f"  providers:\n"
        f"    {PROVIDER_NAME}:\n"
        f"      name: {PROVIDER_NAME}\n"
        f'      base_url: "http://127.0.0.1:4000/v1"\n'
        f"      api_mode: chat_completions\n"
        f'      api_key: "<the LiteLLM master key>"\n'
        f"      default_model: {name}\n"
    )


def cmd_render(args) -> int:
    path = _require_profile(args)
    if not path.is_file():
        raise SystemExit(f"no Hermes profile at {path}")

    info = resolve(args.provider, args.model, Path(args.repo) if args.repo else None)
    original = path.read_text(encoding="utf-8")

    # Read first, decide, then write: a refusal must not leave a half-updated
    # file behind, and the decision needs what is there now.
    _, seen = rewrite(original)
    if seen["provider"] is None:
        raise SystemExit(f"{path} has no `model: provider:` line — is this a Hermes profile?")
    migrating = seen["provider"] != PROVIDER_REF
    if seen["default_model"] is None:
        why = (
            f"{path} runs model.provider: {seen['provider']}, not {PROVIDER_REF}, "
            f"and has no `providers.{PROVIDER_NAME}.default_model` line to move to"
            if migrating
            else f"{path} has no `providers.{PROVIDER_NAME}.default_model` line"
        )
        raise SystemExit(f"{why}. Add the block:\n\n" + _ccp_block(info["name"]))

    # A bot still on a provider ccp replaced (commandcode, openrouter, ...) is
    # the case this command exists for: it is the bot that has to move. Refusing
    # left hand-editing as the only way across. So it moves — and it moves
    # `model.provider` too, because that key is what actually picks the entry
    # point; writing the model name alone would look like a switch and behave
    # like a no-op.
    updated, _ = rewrite(
        original, info["name"], provider=PROVIDER_REF if migrating else None
    )

    changed = updated != original
    if args.json:
        print(json.dumps({**info, "file": str(path), "changed": changed}, indent=2))
        return 0

    print(f"profile:  {_profile_of(args, path) or '(named directly)'}  ({path})")
    if migrating:
        print(f"entry:    {seen['provider']} -> {PROVIDER_REF}   (the bot moves onto ccp)")
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