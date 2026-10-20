#!/usr/bin/env python3
"""Manage several accounts for one provider, and the routing groups over them.

ccp does not own provider accounts — the CLIs do — so "adding an account" here
means *naming* a login and pointing at the env var its credential lives in. The
generated LiteLLM config then emits one deployment per account under the same
model name, and LiteLLM does the rest natively: it load-balances across them and
cools one down when it answers 429, so an exhausted account stops being used
without ccp running a router of its own.

Two files are involved, and only one of them is ccp's:

  registry/profiles.yaml            `accounts:` + `groups:` — the curation layer
  ~/.config/litellm/proxy.env       the credentials themselves (0600)

Keeping the split matters: the registry is committed and shared, the secrets
file never is.

Usage:
  python3 ccp_accounts.py list
  python3 ccp_accounts.py add cline a2 --token sk-…
  python3 ccp_accounts.py remove cline a2
  python3 ccp_accounts.py group add daily cline-deepseek-v4.1-flash --strategy usage-based-routing-v2
  python3 ccp_accounts.py group remove daily
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parent
REPO_DIR = BRIDGE_DIR.parent
PROFILES_YAML = REPO_DIR / "registry" / "profiles.yaml"
PROXY_ENV = Path(os.path.expanduser("~/.config/litellm/proxy.env"))

# Kept in step with render_config.py: these are the strategies LiteLLM accepts.
STRATEGIES = (
    "simple-shuffle",
    "least-busy",
    "latency-based-routing",
    "usage-based-routing-v2",
    "cost-based-routing",
)

SUFFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class AccountsError(Exception):
    """A change that would leave the registry or the secrets file inconsistent."""


# ── profiles.yaml, edited as text so comments survive ─────────────────────
#
# A YAML round-trip through safe_load/dump would delete every comment in the
# file, and that file is mostly comments — it is the documentation for the
# curation layer. So the two keys ccp owns are rewritten in place instead.


def _load_yaml(path: Path) -> dict:
    import yaml

    if not path.is_file():
        raise AccountsError(f"missing {path}")
    return yaml.safe_load(path.read_text()) or {}


def _dump_key(name: str, value) -> str:
    """Render one top-level key as YAML, deterministically."""
    import yaml

    body = yaml.safe_dump(
        {name: value},
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=10**6,
    )
    return body


def _replace_top_level_key(text: str, name: str, rendered: str | None) -> str:
    """Replace (or drop) one top-level YAML key, keeping every other byte.

    ``rendered`` is the full ``name: ...`` block, or None to remove the key.

    The block is the key line plus only its *indented* continuation lines. A
    comment or blank line at column 0 ends it — those belong to the surrounding
    documentation, and treating them as part of the block deleted the whole
    explanatory comment above the next key.
    """
    lines = text.splitlines(keepends=True)
    start = None
    for i, ln in enumerate(lines):
        if re.match(rf"^{re.escape(name)}\s*:", ln):
            start = i
            break
    if start is None:
        if rendered is None:
            return text
        sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        return text + sep + rendered
    end = start + 1
    while end < len(lines) and lines[end].startswith((" ", "\t")):
        end += 1
    if rendered is None:
        return "".join(lines[:start] + lines[end:])
    return "".join(lines[:start]) + rendered + "".join(lines[end:])


def write_registry(accounts: dict, groups: dict) -> None:
    """Persist the two keys ccp owns, atomically, comments intact."""
    text = PROFILES_YAML.read_text()
    text = _replace_top_level_key(text, "accounts", _dump_key("accounts", accounts))
    text = _replace_top_level_key(text, "groups", _dump_key("groups", groups))
    tmp = PROFILES_YAML.with_suffix(".yaml.ccp-tmp")
    tmp.write_text(text)
    os.replace(tmp, PROFILES_YAML)


def load_registry() -> tuple[dict, dict]:
    doc = _load_yaml(PROFILES_YAML)
    return doc.get("accounts") or {}, doc.get("groups") or {}


# ── credentials ───────────────────────────────────────────────────────────


def env_var_for(provider: str, suffix: str) -> str:
    """CLINE_API_KEY_A2 for ('cline', 'a2'), CMDC_API_KEY_EDU for …"""
    return f"{provider.upper()}_API_KEY_{suffix.upper()}"


def read_secret(env_path: Path, key: str) -> str | None:
    """A key from the shared secrets file, or None."""
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return None
    return None


def set_secret(env_path: Path, key: str, value: str) -> None:
    """Replace ``key``'s line, keeping every other provider's entry intact.

    Durable and atomic — temp file in the same directory, fsync, then
    os.replace, mode 0600 — because this file holds every provider's key.
    """
    if env_path.is_file():
        try:
            lines = env_path.read_text().splitlines()
        except OSError as e:
            raise AccountsError(f"cannot read {env_path}: {e} (refusing to overwrite)")
    else:
        lines = []
    out, replaced = [], False
    for line in lines:
        if line.strip().startswith(key + "="):
            if not replaced:
                out.append(f"{key}={value}")
                replaced = True
            continue
        out.append(line)
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append(f"{key}={value}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(env_path.parent), prefix="." + env_path.name + ".")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(out) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, env_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── commands ──────────────────────────────────────────────────────────────


def cmd_list(args) -> int:
    accounts, groups = load_registry()
    env_path = Path(os.path.expanduser(args.proxy_env))

    if not accounts:
        print("No extra accounts configured (single-account behaviour).")
    for provider, entries in accounts.items():
        print(f"\n{provider}")
        primary = f"{provider.upper()}_API_KEY"
        print(f"  {'(primary)':12s} {primary:28s} {'set' if read_secret(env_path, primary) else 'MISSING'}")
        for suffix, spec in (entries or {}).items():
            env = (spec or {}).get("auth_env") or env_var_for(provider, suffix)
            label = (spec or {}).get("label") or ""
            state = "set" if read_secret(env_path, env) else "MISSING"
            print(f"  {suffix:12s} {env:28s} {state}{f'  ({label})' if label else ''}")
        print("  → each account is another deployment of the same model names; LiteLLM")
        print("    spreads load over them and skips one that answers 401/429")

    if not groups:
        print("\nNo routing groups configured.")
    for name, spec in groups.items():
        members = ", ".join(spec.get("members") or [])
        strat = spec.get("routing_strategy") or "simple-shuffle"
        fb = ", ".join(spec.get("fallback") or []) or "-"
        print(f"\ngroup/{name}  [{strat}]")
        print(f"  members:  {members}")
        print(f"  fallback: {fb}")
    return 0


def cmd_add(args) -> int:
    provider, suffix = args.provider, args.suffix
    if not SUFFIX_RE.match(suffix):
        raise AccountsError(f"bad suffix {suffix!r}: use letters, digits, - and _")
    accounts, groups = load_registry()
    pacc = accounts.setdefault(provider, {})
    if suffix in pacc:
        raise AccountsError(f"{provider} already has an account named {suffix!r}")

    env = args.auth_env or env_var_for(provider, suffix)
    if not args.token:
        raise AccountsError(
            "an account needs --token: ccp no longer imports a login itself "
            "(that was CodeBuddy's, and its provider is archived)"
        )
    token, label = args.token, (args.label or "")

    env_path = Path(os.path.expanduser(args.proxy_env))
    set_secret(env_path, env, token)
    pacc[suffix] = {"auth_env": env}
    if args.label or label:
        pacc[suffix]["label"] = args.label or label
    write_registry(accounts, groups)

    print(f"  ✔ {provider}/{suffix}")
    print(f"      credential: {env} in {env_path}")
    print(f"      registry:   registry/profiles.yaml accounts.{provider}.{suffix}")
    print("  Run `ccp render` (or `ccp bridge restart`, which renders' output is deployed by) to apply.")
    return 0


def cmd_remove(args) -> int:
    provider, suffix = args.provider, args.suffix
    accounts, groups = load_registry()
    pacc = accounts.get(provider) or {}
    if suffix not in pacc:
        raise AccountsError(f"{provider} has no account named {suffix!r}")
    env = (pacc.pop(suffix) or {}).get("auth_env") or env_var_for(provider, suffix)
    if not pacc:
        accounts.pop(provider, None)
    write_registry(accounts, groups)

    print(f"  ✔ removed {provider}/{suffix} from the registry")
    print(f"      {env} is still in the secrets file — remove it by hand if the")
    print("      login is gone for good (ccp never rewrites secrets it cannot read).")
    return 0


def cmd_group_add(args) -> int:
    if args.strategy and args.strategy not in STRATEGIES:
        raise AccountsError(f"unknown strategy {args.strategy!r}; pick one of {', '.join(STRATEGIES)}")
    accounts, groups = load_registry()
    spec: dict = {"members": list(args.members)}
    if args.strategy:
        spec["routing_strategy"] = args.strategy
    if args.fallback:
        spec["fallback"] = list(args.fallback)
    groups[args.name] = spec
    write_registry(accounts, groups)
    print(f"  ✔ group/{args.name} -> {', '.join(args.members)}")
    if args.fallback:
        print(f"      falls back to: {', '.join(args.fallback)}")
    print(f"  Use it with: ccp -m group/{args.name}")
    return 0


def cmd_group_remove(args) -> int:
    accounts, groups = load_registry()
    if args.name not in groups:
        raise AccountsError(f"no group named {args.name!r}")
    groups.pop(args.name)
    write_registry(accounts, groups)
    print(f"  ✔ removed group/{args.name}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--proxy-env", default=str(PROXY_ENV))
    sub = ap.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("list", help="show accounts, credentials and groups")
    lp.set_defaults(func=cmd_list)

    a = sub.add_parser("add", help="name an extra account for a provider")
    a.add_argument("provider")
    a.add_argument("suffix", help="short name, e.g. a2 or edu")
    a.add_argument("--token", help="the credential itself")
    a.add_argument("--auth-env", help="override the env var name")
    a.add_argument("--label", help="a human note, stored in the registry")
    a.set_defaults(func=cmd_add)

    r = sub.add_parser("remove", help="forget an account")
    r.add_argument("provider")
    r.add_argument("suffix")
    r.set_defaults(func=cmd_remove)

    g = sub.add_parser("group", help="manage routing groups")
    gsub = g.add_subparsers(dest="gcmd", required=True)
    ga = gsub.add_parser("add")
    ga.add_argument("name")
    ga.add_argument("members", nargs="+", help="aliases or registry model ids")
    ga.add_argument("--strategy", choices=STRATEGIES)
    ga.add_argument("--fallback", nargs="+", help="name(s) to use when all members are out")
    ga.set_defaults(func=cmd_group_add)
    gr = gsub.add_parser("remove")
    gr.add_argument("name")
    gr.set_defaults(func=cmd_group_remove)

    args = ap.parse_args()
    try:
        return args.func(args)
    except AccountsError as e:
        print(f"  ✘ {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
