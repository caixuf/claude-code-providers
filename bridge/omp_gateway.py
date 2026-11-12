#!/usr/bin/env python3
"""Adapt omp's credential broker/gateway to a Claude Code profile.

omp already owns the thing ccp deliberately does not: provider logins, OAuth
refresh, and several accounts per provider. Its `auth-broker` keeps the real
credentials and refreshes them; its `auth-gateway` exposes an Anthropic
Messages endpoint on loopback that resolves the account for each request.

So ccp does not import or refresh a token here. It points Claude Code at that
gateway and keeps only the gateway bearer token. The account pool, quota and
rotation stay in one place: omp.

Commands
--------
  status                     broker/gateway health, token presence, account count
  setup [--no-start]         install + enable the two user units
  start|stop|restart         manage those units
  login [provider]           run `omp auth-broker login`
  accounts                   run `omp usage` (per-account quota)
  upstreams [--json]         list the provider layer (opencode, anthropic, ...)
  models [upstream] [--json] list what the gateway serves
  profile [model] --upstream U --out P
                             emit a Claude Code profile for one upstream

The model names are omp's exact ids (`provider/model`). The gateway does not
accept omp role selectors such as `@smol` on its HTTP endpoint; those only work
in omp's own CLI.

The layer is the provider: `opencode-go/...` and `opencode-zen/...` both fold
into the `opencode` upstream, so `ccp omp opencode` is a sibling of the direct
`ccp opencode` profile rather than one gateway-wide profile above every vendor.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BROKER = "http://127.0.0.1:8765"
DEFAULT_GATEWAY = "http://127.0.0.1:4010"
BROKER_UNIT = "omp-auth-broker.service"
GATEWAY_UNIT = "omp-auth-gateway.service"
UNIT_DIR = Path.home() / ".config/systemd/user"
DYNAMIC_SUFFIX = ".ccp-dynamic"

# omp's account list is broader than ccp's curated providers. These are the
# families a Claude Code tier should reach for when the user did not name one.
PROVIDER_PRIORITY = (
    "anthropic",
    "google-antigravity",
    "openai-codex",
    "openai",
    "google-gemini-cli",
    "zai",
    "zai-coding-plan",
    "kimi-code",
    "github-copilot",
    "xai",
    "deepseek",
    "qianfan",
)


def omp_bin() -> str:
    found = shutil.which("omp")
    if found:
        return found
    candidate = Path.home() / ".local/bin/omp"
    if candidate.is_file():
        return str(candidate)
    raise SystemExit("omp not found (install omp or put it on PATH)")


def gateway_url() -> str:
    return os.environ.get("OMP_AUTH_GATEWAY_URL", DEFAULT_GATEWAY).rstrip("/")


def broker_url() -> str:
    return os.environ.get("OMP_AUTH_BROKER_URL", DEFAULT_BROKER).rstrip("/")


def gateway_token() -> str:
    """The bearer Claude Code will send; never the provider credential."""
    token = os.environ.get("OMP_AUTH_GATEWAY_TOKEN", "").strip()
    if token:
        return token
    try:
        out = subprocess.run(
            [omp_bin(), "auth-gateway", "token"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _get(path: str, token: str = "", timeout: float = 10.0):
    url = gateway_url() + path
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def health() -> bool:
    try:
        _get("/healthz", timeout=2.0)
        return True
    except Exception:
        return False


def fetch_models() -> list[dict]:
    token = gateway_token()
    if not token:
        raise SystemExit(
            "no omp auth-gateway token; run `ccp omp setup` and `ccp omp login`"
        )
    try:
        doc = _get("/v1/models", token)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"omp gateway /v1/models -> HTTP {exc.code}") from exc
    except Exception as exc:
        raise SystemExit(
            f"omp gateway is not reachable at {gateway_url()} ({exc}); "
            "run `ccp omp setup`"
        ) from exc
    return [m for m in doc.get("data", []) if isinstance(m, dict) and m.get("id")]


def ctx_of(model: dict) -> int | None:
    value = model.get("context_length")
    return int(value) if isinstance(value, (int, float)) and value > 0 else None


def vision_of(model: dict) -> bool | None:
    modalities = model.get("input_modalities")
    if not isinstance(modalities, list):
        return None
    return "image" in modalities


def _provider(model_id: str) -> str:
    return model_id.split("/", 1)[0] if "/" in model_id else ""


def upstream_of(model_id: str) -> str:
    """The provider layer a gateway model belongs to.

    omp exposes `opencode-go/...` and `opencode-zen/...` as separate ids, but
    for ccp they are one provider layer: `ccp omp opencode` should be a sibling
    of `ccp opencode`, not two folders that make the user choose a plan name.
    """
    provider = _provider(model_id)
    return "opencode" if provider.startswith("opencode-") else provider


def models_for_upstream(models: list[dict], upstream: str | None) -> list[dict]:
    if not upstream or upstream == "all":
        return models
    return [m for m in models if upstream_of(m["id"]) == upstream]


def upstreams(models: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for model in models:
        out.setdefault(upstream_of(model["id"]), []).append(model)
    return dict(sorted(out.items()))


def _provider_rank(model_id: str, wanted: tuple[str, ...]) -> tuple[int, int]:
    provider = _provider(model_id)
    try:
        base = wanted.index(provider)
    except ValueError:
        base = len(wanted) + 1
    return base, len(model_id)


def _pick(models: list[dict], needles: tuple[str, ...], wanted: tuple[str, ...]) -> dict | None:
    hits = [
        m for m in models
        if any(n in m["id"].lower() for n in needles)
    ]
    if not hits:
        return None
    hits.sort(key=lambda m: _provider_rank(m["id"], wanted))
    return hits[0]


def pick_models(
    models: list[dict],
    requested: str | None = None,
    upstream: str | None = None,
) -> dict[str, dict]:
    """Choose one model per Claude Code tier from the gateway's catalogue.

    A requested model wins the default/sonnet/fable slots. The other tiers are
    best-effort family picks: opus prefers an `opus` id, haiku prefers
    `haiku`/`flash`. When a family is absent the primary model backs the slot,
    which is safer than inventing a model name the gateway will 404.

    For OpenCode Go, the useful default is its cheap DeepSeek Flash, not the
    GPT rung that happens to sort first — the plan is a flat subscription, but
    a coding-agent session should still start on the model the ccp registry
    curated for that plan.
    """
    by_id = {m["id"]: m for m in models}

    if requested:
        if requested in by_id:
            primary = by_id[requested]
        else:
            needle = requested.lower()
            hits = [m for m in models if needle in m["id"].lower()]
            if not hits:
                raise SystemExit(
                    f"'{requested}' is not served by omp. Run `ccp omp models`."
                )
            hits.sort(key=lambda m: _provider_rank(m["id"], PROVIDER_PRIORITY))
            primary = hits[0]
    elif upstream == "opencode":
        primary = (
            _pick(models, ("deepseek-v4.1-flash",), PROVIDER_PRIORITY)
            or _pick(models, ("kimi-k3",), PROVIDER_PRIORITY)
            or _pick(models, ("glm-5.3-flash",), PROVIDER_PRIORITY)
            or _pick(models, ("claude-sonnet",), PROVIDER_PRIORITY)
            or models[0]
        )
    else:
        primary = (
            _pick(models, ("claude-sonnet",), PROVIDER_PRIORITY)
            or _pick(models, ("gpt-5", "gpt-4"), PROVIDER_PRIORITY)
            or _pick(models, ("gemini",), PROVIDER_PRIORITY)
            or models[0]
        )

    if upstream == "opencode":
        opus = (
            _pick(models, ("kimi-k3",), PROVIDER_PRIORITY)
            or _pick(models, ("claude-opus", "opus"), PROVIDER_PRIORITY)
            or primary
        )
    else:
        opus = _pick(models, ("opus",), PROVIDER_PRIORITY) or primary
    haiku = (
        _pick(models, ("haiku", "flash"), PROVIDER_PRIORITY)
        or primary
    )
    return {
        "default": primary,
        "sonnet": primary,
        "fable": primary,
        "opus": opus,
        "haiku": haiku,
    }


def profile_for(
    models: list[dict],
    requested: str | None = None,
    upstream: str | None = None,
) -> dict:
    pool = models_for_upstream(models, upstream)
    if not pool:
        known = ", ".join(upstreams(models)) or "none"
        raise SystemExit(f"omp has no models for upstream '{upstream}' (known: {known})")
    chosen = pick_models(pool, requested, upstream)
    contexts = [ctx_of(m) for m in chosen.values() if ctx_of(m)]
    # One context number per Claude Code session, so advertise the smallest
    # window in the tier set. The gateway may route the same id differently
    # when accounts change, and a number the weakest tier cannot honour is
    # worse than a smaller number that always works.
    ctx = min(contexts) if contexts else 200000
    token = gateway_token()
    if not token:
        raise SystemExit(
            "no omp auth-gateway token; run `ccp omp setup` and `ccp omp login`"
        )
    env = {
        "ANTHROPIC_AUTH_TOKEN": token,
        "ANTHROPIC_BASE_URL": gateway_url(),
        "ANTHROPIC_MODEL": chosen["default"]["id"],
        "ANTHROPIC_DEFAULT_OPUS_MODEL": chosen["opus"]["id"],
        "ANTHROPIC_DEFAULT_FABLE_MODEL": chosen["fable"]["id"],
        "ANTHROPIC_DEFAULT_SONNET_MODEL": chosen["sonnet"]["id"],
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": chosen["haiku"]["id"],
        "ANTHROPIC_SMALL_FAST_MODEL": chosen["haiku"]["id"],
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(ctx),
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(ctx),
    }
    return {
        "env": env,
        "permissions": {"defaultMode": "bypassPermissions"},
        "model": chosen["default"]["id"],
        "autoUpdatesChannel": "stable",
        "skipDangerousModePermissionPrompt": True,
        "theme": "dark",
    }


def write_dynamic(path: Path, profile: dict, upstream: str | None = None) -> None:
    """Write a profile and the sidecar that says 'ccp omp owns this file'.

    `ccp sync` must not reconcile a dynamic profile back to a template, and
    `profile_audit` must not call it dead just because its model names are
    omp's, not ccp's bridge's.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    path.chmod(0o600)
    sidecar = path.with_name(path.name + DYNAMIC_SUFFIX)
    sidecar.write_text(
        json.dumps({"source": "omp", "upstream": upstream or "all"}) + "\n",
        encoding="utf-8",
    )
    sidecar.chmod(0o600)


def sync_gateway_token() -> Path:
    """Expose the gateway bearer to LiteLLM without copying a provider token.

    The groups' omp deployments use `os.environ/OMP_AUTH_GATEWAY_TOKEN`, which
    systemd reads from proxy.env. omp remains the writer of the real credential;
    this only mirrors its gateway token into the file LiteLLM loads.
    """
    token = gateway_token()
    if not token:
        raise SystemExit(
            "no omp auth-gateway token; run `ccp omp setup` first"
        )
    from ccp_accounts import PROXY_ENV, set_secret

    set_secret(PROXY_ENV, "OMP_AUTH_GATEWAY_TOKEN", token)
    return PROXY_ENV


def render_models(models: list[dict], as_json: bool = False) -> str:
    if as_json:
        return json.dumps(
            [
                {
                    "id": m["id"],
                    "ctx": ctx_of(m),
                    "max_output": m.get("max_output_tokens"),
                    "vision": vision_of(m),
                }
                for m in models
            ],
            indent=2,
            ensure_ascii=False,
        )
    lines = [f"omp auth-gateway — {len(models)} models at {gateway_url()}",
             f"  {'model':52s} {'ctx':>7} {'out':>8}  vis"]
    for m in models:
        ctx = ctx_of(m)
        ctx_s = "?" if not ctx else (f"{ctx / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
                                     if ctx >= 1_000_000 else f"{ctx // 1000}k")
        vis = "yes" if vision_of(m) is True else ("no" if vision_of(m) is False else "-")
        out = m.get("max_output_tokens") or "?"
        lines.append(f"  {m['id']:52s} {ctx_s:>7} {str(out):>8}  {vis}")
    return "\n".join(lines)


def unit_text(omp: str) -> tuple[str, str]:
    broker = f"""[Unit]
Description=omp auth-broker (credential vault)
After=network.target

[Service]
Type=simple
ExecStart={omp} auth-broker serve --bind=127.0.0.1:8765
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""
    gateway = f"""[Unit]
Description=omp auth-gateway (Anthropic/OpenAI facade over the broker)
After=network.target omp-auth-broker.service
Requires=omp-auth-broker.service

[Service]
Type=simple
ExecStart={omp} auth-gateway serve --bind=127.0.0.1:4010
Restart=always
RestartSec=3
Environment=OMP_AUTH_BROKER_URL=http://127.0.0.1:8765
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""
    return broker, gateway


def systemctl(*args: str) -> int:
    return subprocess.run(["systemctl", "--user", *args], check=False).returncode


def cmd_status(args: argparse.Namespace) -> int:
    ok = health()
    token = gateway_token()
    print(f"omp:            {omp_bin()}")
    print(f"broker:         {broker_url()}")
    print(f"gateway:        {gateway_url()}  {'healthy' if ok else 'NOT HEALTHY'}")
    print(f"gateway token:  {'present' if token else 'missing'}")
    for unit in (BROKER_UNIT, GATEWAY_UNIT):
        state = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True, text=True,
        ).stdout.strip() or "unknown"
        print(f"{unit:24s} {state}")
    if ok and token:
        try:
            models = fetch_models()
            print(f"models:         {len(models)}")
        except SystemExit as exc:
            print(f"models:         {exc}")
    return 0 if ok and token else 1


def cmd_setup(args: argparse.Namespace) -> int:
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    broker, gateway = unit_text(omp_bin())
    (UNIT_DIR / BROKER_UNIT).write_text(broker, encoding="utf-8")
    (UNIT_DIR / GATEWAY_UNIT).write_text(gateway, encoding="utf-8")
    print(f"Installed {UNIT_DIR / BROKER_UNIT}")
    print(f"Installed {UNIT_DIR / GATEWAY_UNIT}")
    systemctl("daemon-reload")
    if args.no_start:
        print("Not started (--no-start).")
        return 0
    systemctl("enable", "--now", BROKER_UNIT, GATEWAY_UNIT)
    try:
        path = sync_gateway_token()
        print(f"Mirrored gateway token -> {path}")
        print("Run `ccp bridge restart` so LiteLLM picks it up for group fallback.")
    except SystemExit as exc:
        print(f"gateway token not mirrored yet: {exc}", file=sys.stderr)
    return cmd_status(argparse.Namespace())


def cmd_sync_token(args: argparse.Namespace) -> int:
    print(f"Mirrored gateway token -> {sync_gateway_token()}")
    print("Run `ccp bridge restart` so LiteLLM picks it up.")
    return 0


def cmd_service(args: argparse.Namespace) -> int:
    for unit in (BROKER_UNIT, GATEWAY_UNIT):
        if args.action == "stop":
            systemctl("stop", unit)
        elif args.action == "start":
            systemctl("start", unit)
        else:
            systemctl("restart", unit)
    if args.action == "stop":
        return 0
    return cmd_status(argparse.Namespace())


def cmd_login(args: argparse.Namespace) -> int:
    cmd = [omp_bin(), "auth-broker", "login"]
    if args.provider:
        cmd.append(args.provider)
    return subprocess.run(cmd, check=False).returncode


def cmd_accounts(args: argparse.Namespace) -> int:
    return subprocess.run([omp_bin(), "usage"], check=False).returncode


def cmd_models(args: argparse.Namespace) -> int:
    models = models_for_upstream(fetch_models(), args.upstream)
    print(render_models(models, as_json=args.json))
    return 0


def cmd_upstreams(args: argparse.Namespace) -> int:
    """List the provider layer, not omp's individual login plans."""
    grouped = upstreams(fetch_models())
    if args.json:
        print(json.dumps(
            {name: [m["id"] for m in models] for name, models in grouped.items()},
            indent=2,
            ensure_ascii=False,
        ))
        return 0
    print(f"omp auth-gateway — {len(grouped)} upstreams at {gateway_url()}")
    print(f"  {'upstream':30s} {'models':>6}  example")
    for name, models in grouped.items():
        print(f"  {name:30s} {len(models):>6}  {models[0]['id']}")
    print("\nSwitch with: ccp omp <upstream> [model]   (e.g. ccp omp opencode)")
    return 0


def cmd_profile(args: argparse.Namespace) -> int:
    models = fetch_models()
    profile = profile_for(models, args.model, args.upstream)
    text = json.dumps(profile, indent=2) + "\n"
    if args.out:
        write_dynamic(Path(args.out), profile, args.upstream)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ccp omp")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="broker/gateway health and token presence")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("setup", help="install and start the two user units")
    s.add_argument("--no-start", action="store_true")
    s.set_defaults(func=cmd_setup)

    for action in ("start", "stop", "restart"):
        s = sub.add_parser(action, help=f"{action} the omp services")
        s.set_defaults(func=cmd_service, action=action)

    s = sub.add_parser("login", help="run omp auth-broker login")
    s.add_argument("provider", nargs="?", help="omp provider id; omit to pick")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("accounts", help="run omp usage (per-account quota)")
    s.set_defaults(func=cmd_accounts)

    s = sub.add_parser("models", help="list the gateway's models")
    s.add_argument("upstream", nargs="?")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_models)

    s = sub.add_parser("upstreams", help="list the provider layer")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_upstreams)

    s = sub.add_parser("sync-token", help="mirror the gateway token into proxy.env")
    s.set_defaults(func=cmd_sync_token)

    s = sub.add_parser("profile", help="emit a Claude Code profile")
    s.add_argument("model", nargs="?")
    s.add_argument("--upstream")
    s.add_argument("--out")
    s.set_defaults(func=cmd_profile)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
