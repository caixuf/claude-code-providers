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
import socket
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent
sys.path.insert(0, str(BRIDGE))

import bridge_auth  # noqa: E402
import profile_audit  # noqa: E402

MODEL_KEYS = profile_audit.MODEL_KEYS


def hermes_home() -> Path:
    """Read at call time: the home is an environment override, not a constant."""
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


#: A YAML key line, optionally opening a list item (``- provider: …``).
_KEY_LINE = re.compile(
    r"^(?P<indent> *)(?:- +)?(?P<key>[A-Za-z_][\w.-]*):(?P<gap>[ \t]*)(?P<rest>.*)$"
)

#: Blocks whose ``model:`` key names a model the bridge is expected to serve.
#: ``stt`` has a ``model:`` too (``base``, ``whisper-1``) — those are speech
#: models, and asking the bridge for them reports a healthy machine as broken.
_MODEL_BLOCKS = {"fallback_model", "auxiliary", "delegation"}


def _active_profile() -> str | None:
    """The profile named in ``active_profile``, if the install has one."""
    try:
        name = (hermes_home() / "active_profile").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return name or None


def _installed_bot_profiles() -> set[str]:
    """Profiles Hermes actually runs: one systemd unit per bot.

    A profile directory is cheap to leave behind — a test fixture, a retired
    bot, a one-off experiment. "There is a config.yaml" is therefore not
    evidence that anybody runs it, and auditing a fixture reports a healthy
    machine as broken: the identity check deliberately points an isolated
    profile at ``gpt-4o-mini`` with an invalid key, to force one API call.
    """
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    prefix, suffix = "hermes-gateway-", ".service"
    names: set[str] = set()
    try:
        for path in unit_dir.glob(prefix + "*" + suffix):
            names.add(path.name[len(prefix):-len(suffix)])
    except OSError:
        pass
    return names


def _hermes_configs() -> list[Path]:
    """The Hermes config: one file for a single-config install, one per profile
    otherwise. The bot is whatever profile is actually run, so the profiles
    with a gateway unit — plus the one named in ``active_profile`` — are the
    ones that matter. A bare install says neither, and there we report them all
    rather than none."""
    profiles = hermes_home() / "profiles"
    if not profiles.is_dir():
        single = hermes_home() / "config.yaml"
        return [single] if single.is_file() else []
    configs = {p.parent.name: p for p in profiles.glob("*/config.yaml")}
    running = _installed_bot_profiles() & set(configs)
    active = _active_profile()
    if active in configs:
        running.add(active)
    return [configs[name] for name in sorted(running)] or sorted(configs.values())


def _append_once(out: list[str], value: str) -> None:
    if value and value not in out:
        out.append(value)


def hermes_models(path: Path) -> list[str]:
    """The model names a Hermes config asks for, read without a YAML parser.

    The file is hand-written and heavily commented; a parser would need the
    whole document to be valid to answer one question, and a Hermes config
    that is halfway edited must still be reportable.

    Only keys that carry a model count. ``default:`` is a model under the
    top-level ``model:`` block, but a *route name* under
    ``delegation.backends.model_map:`` — ``default: deep`` selects an option
    whose own ``model: main`` is what the bridge serves, so keying on the name
    alone reported every bot as dead for a model it never sends upstream.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[str] = []
    stack: list[tuple[int, str]] = []  # (indent, key), outermost first
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _KEY_LINE.match(line)
        if not m:
            continue
        indent = len(m.group("indent"))
        key = m.group("key")
        value = m.group("rest").split("#")[0].strip().strip("'\"")
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1] if stack else None
        top = stack[0][1] if stack else None
        if value:
            if key == "default_model":
                _append_once(out, value)
            elif key == "default" and parent == "model":
                _append_once(out, value)
            elif key == "model" and top in _MODEL_BLOCKS:
                _append_once(out, value)
        stack.append((indent, key))
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


# ---------------------------------------------------------------------------
# What the machine left behind. The install is a tool, not a museum: backups,
# duplicate secrets and a dead proxy are the three ways it silently stops
# being the thing the repository describes (PRINCIPLES.md §1 §2 §4).
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "litellm"
_PROXY_VARS = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines. An unreadable file is empty, never an exception."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip():
            out[key.strip()] = value.strip()
    return out


def backup_litter(config_dir: Path) -> tuple[list[str], list[str]]:
    """Backups are temporary; the config directory is not an archive.

    Returns ``(problems, warnings)``. A ``proxy.env`` backup is a *problem*:
    the README promises none, because a key revoked in the last write must not
    come back by restoring a file. Everything else is a warning past one copy.
    """
    problems: list[str] = []
    warnings: list[str] = []
    groups: dict[str, list[Path]] = {}
    try:
        entries = list(config_dir.iterdir())
    except OSError:
        return problems, warnings
    for path in entries:
        if not path.is_file() or ".bak" not in path.name:
            continue
        groups.setdefault(path.name.split(".bak", 1)[0], []).append(path)
    for base, paths in sorted(groups.items()):
        if base == "proxy.env":
            problems.append(
                f"{config_dir}/{base} has {len(paths)} backup(s); a secret must not be "
                "restorable from a backup (PRINCIPLES.md §2)"
            )
        elif len(paths) > 1:
            warnings.append(
                f"{config_dir}/{base} has {len(paths)} backups; keep at most one "
                "(older copies are history, not assets — PRINCIPLES.md §2)"
            )
    return problems, warnings


def duplicate_secrets(env: dict[str, str]) -> list[str]:
    """One provider, one variable name.

    Two names holding the same secret is how a rotation lands in one place and
    not the other: on 2026-11-04 ``MINIMAX_PLAN_API_KEY`` was overwritten with
    ``MINIMAX_API_KEY``'s value, the primary group started answering 402, and
    LiteLLM fell back to another vendor without saying so.
    """
    by_value: dict[str, list[str]] = {}
    for key, value in env.items():
        if key.lower() in _PROXY_VARS or not value or value.startswith("YOUR_"):
            continue
        by_value.setdefault(value, []).append(key)
    out: list[str] = []
    for names in by_value.values():
        if len(names) > 1:
            out.append(
                " and ".join(sorted(names))
                + " hold the same secret; one provider, one variable (PRINCIPLES.md §1)"
            )
    return out


def proxy_endpoint(env: dict[str, str]) -> tuple[str, int] | None:
    """The ``host:port`` the file points provider traffic at, if any."""
    raw = (
        env.get("https_proxy")
        or env.get("HTTPS_PROXY")
        or env.get("http_proxy")
        or env.get("HTTP_PROXY")
    )
    if not raw:
        return None
    parsed = urllib.parse.urlsplit(raw if "://" in raw else "http://" + raw)
    if not parsed.hostname:
        return None
    return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def proxy_port_alive(env: dict[str, str], *, timeout: float = 1.0) -> tuple[bool, str]:
    """A configured proxy that is not listening fails every call ~15s at a time."""
    endpoint = proxy_endpoint(env)
    if endpoint is None:
        return True, "no proxy configured"
    host, port = endpoint
    try:
        with socket.create_connection(endpoint, timeout=timeout):
            return True, f"{host}:{port} accepts connections"
    except OSError as exc:
        return False, f"{host}:{port} does not accept connections ({type(exc).__name__})"


def model_family(name: str) -> str:
    """``foo[1m]`` and ``foo`` are the same model: the marker only selects the
    long-context deployment, and the served name comes back without it."""
    return re.sub(r"\[1m\]$", "", name)


def probe_model(
    bridge: str,
    master_key: str | None,
    model: str,
    *,
    timeout: float = 30.0,
    urlopen=urllib.request.urlopen,
) -> tuple[str | None, str | None]:
    """Ask for one token; report the model LiteLLM says actually answered.

    A fallback is invisible from the caller's side otherwise: the request
    succeeds, the answer is plausible, and a retired primary is never noticed.
    """
    body = json.dumps(
        # Not 1: some Anthropic-compatible endpoints reject a one-token budget,
        # and the probe would report a fallback that no real request triggers.
        {"model": model, "max_tokens": 8, "messages": [{"role": "user", "content": "ok"}]}
    ).encode()
    headers = {"Content-Type": "application/json"}
    if master_key:
        headers["Authorization"] = "Bearer " + master_key
    request = urllib.request.Request(
        bridge.rstrip("/") + "/v1/chat/completions", data=body, headers=headers, method="POST"
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 - the failure *is* the finding
        return None, f"{type(exc).__name__}: {exc}"
    served = payload.get("model")
    return (served if isinstance(served, str) and served else None), None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--providers-dir", default=str(Path.home() / ".claude" / "providers"))
    ap.add_argument("--settings", default=str(Path.home() / ".claude" / "settings.json"))
    ap.add_argument("--repo", default=str(BRIDGE.parent))
    ap.add_argument("--bridge", default=profile_audit.DEFAULT_BRIDGE)
    ap.add_argument(
        "--bridge-attempts",
        type=int,
        default=3,
        help="how many times to wait for a bridge that is still starting (default 3)",
    )
    ap.add_argument("--bridge-delay", type=float, default=2.0)
    ap.add_argument("--config-dir", default=str(DEFAULT_CONFIG_DIR))
    ap.add_argument(
        "--probe",
        action="store_true",
        help="send one minimal request and report the model that actually answered",
    )
    ap.add_argument("--probe-model", default=None)
    ap.add_argument("--probe-timeout", type=float, default=30.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    repo = Path(args.repo).expanduser()
    bridge_notes: list[str] = []
    served = profile_audit.served_names(
        repo,
        args.bridge or None,
        attempts=args.bridge_attempts,
        delay=args.bridge_delay,
        error_out=bridge_notes,
    )
    problems: list[str] = []
    warnings: list[str] = []

    # A bridge that is still starting is not a dead model. Say which one it is
    # instead of blaming every profile for a machine that is merely late.
    if bridge_notes:
        problems.append(
            "; ".join(bridge_notes)
            + ". Its model list is unknown, so dead-model findings are withheld: "
            "they would blame the models for a bridge that has not finished starting."
        )

    # The credential the bridge actually accepts: the master key in the
    # config LiteLLM runs on. A client that carries another one still gets
    # through - the bridge forwards this one - so a mismatch is a stale
    # config, not a broken one.
    master_key, key_source = bridge_auth.load_master_key()

    profiles = profile_audit.rows_for(Path(args.providers_dir).expanduser(), served)
    dead = [] if bridge_notes else [row for row in profiles if row["dead"]]
    for row in dead:
        problems.append(
            f"profile {row['profile']} names {', '.join(row['missing'])}, "
            "which the bridge no longer serves"
        )

    # A profile ccp generated from a live service (currently `ccp omp`) names
    # that service's models, not the bridge's. Do not call those dead just
    # because the bridge cannot resolve them.
    dynamic_models: set[str] = set()
    providers_dir = Path(args.providers_dir).expanduser()
    for sidecar in providers_dir.glob("*.json.ccp-dynamic"):
        dynamic_models.update(profile_audit.profile_models(sidecar.with_name(sidecar.name[: -len(".ccp-dynamic")])))

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
    if (
        active_model
        and active_model not in served
        and active_model not in dynamic_models
        and not bridge_notes
    ):
        problems.append(f"the active profile asks for {active_model}, which the bridge no longer serves")
    if bridge_auth.is_bridge_url(active_base) and active_token != master_key:
        warnings.append(
            "the active profile points at the bridge with a credential that is not the "
            f"bridge's key (it will be replaced, so calls still work; `ccp sync` repairs it)"
        )

    bots = []
    for cfg in _hermes_configs():
        models = hermes_models(cfg)
        missing = [] if bridge_notes else [m for m in models if m not in served]
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

    # The machine's own state: litter, secret hygiene, the proxy it points at.
    config_dir = Path(args.config_dir).expanduser()
    proxy_report: dict[str, object] = {}
    proxy_env = read_env_file(config_dir / "proxy.env")
    if config_dir.is_dir():
        litter_problems, litter_warnings = backup_litter(config_dir)
        problems.extend(litter_problems)
        warnings.extend(litter_warnings)
        for duplicate in duplicate_secrets(proxy_env):
            warnings.append(duplicate)
        alive, detail = proxy_port_alive(proxy_env)
        proxy_report = {"detail": detail, "alive": alive}
        if not alive:
            problems.append(
                f"the proxy in {config_dir}/proxy.env is dead: {detail}; every provider "
                "call will hang until it times out (PRINCIPLES.md §4)"
            )

    probe_report = None
    if args.probe:
        target = args.probe_model or active_model or (sorted(served)[0] if served else None)
        if not target:
            warnings.append("--probe was asked for, but there is no model name to ask for")
        else:
            answered, error = probe_model(
                args.bridge or profile_audit.DEFAULT_BRIDGE,
                master_key,
                target,
                timeout=args.probe_timeout,
            )
            probe_report = {"requested": target, "served": answered, "error": error}
            if error:
                problems.append(f"probing {target} through {args.bridge} failed: {error}")
            elif answered and model_family(answered) != model_family(target):
                warnings.append(
                    f"{target} was answered by {answered} — a fallback is serving, the "
                    "primary is out (PRINCIPLES.md §5)"
                )

    if args.json:
        print(json.dumps(
            {
                "served": len(served),
                "profiles": profiles,
                "active_model": active_model,
                "bots": bots,
                "proxy": proxy_report,
                "probe": probe_report,
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
    if proxy_report:
        print(f"proxy: {proxy_report['detail']}")
    if probe_report:
        if probe_report["error"]:
            print(f"probe: {probe_report['requested']} -> error: {probe_report['error']}")
        else:
            print(f"probe: {probe_report['requested']} -> {probe_report['served']}")
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
