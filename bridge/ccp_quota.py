#!/usr/bin/env python3
"""Show what is left on each provider's plan, where the provider exposes it.

Quota is what makes a pool useful: knowing an account is nearly out is what
tells you to add another one, and knowing which is fullest is what tells you
which to make primary.

Not every provider publishes a usage API, and ccp does not invent one. Each
provider below declares the endpoint and the shape of its answer; a provider
with no entry is reported as "no usage API" rather than guessed at.

  codebuddy   POST /v2/billing/meter/get-user-resource  (credits, cycle window)
  cline       GET  /api/v1/users/me                     (identity only today)
  others      — no public usage endpoint

A reading is cached briefly, because these endpoints are rate limited and
`ccp quota` is cheap to type.

Usage:
  python3 ccp_quota.py            # every provider that can be read
  python3 ccp_quota.py codebuddy  # just one
  python3 ccp_quota.py --json     # machine-readable
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parent
PROXY_ENV = Path(os.path.expanduser("~/.config/litellm/proxy.env"))
CACHE_DIR = Path(os.path.expanduser("~/.cache/ccp"))
CACHE_TTL = 120  # seconds; these endpoints are rate limited

sys.path.insert(0, str(BRIDGE_DIR))


def _token(env_name: str) -> str | None:
    import codebuddy_auth as cb

    return cb.read_env_value(PROXY_ENV, env_name)


def _post_json(url: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context()) as r:
        return json.loads(r.read().decode("utf-8"))


def _get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context()) as r:
        return json.loads(r.read().decode("utf-8"))


# ── per-provider readers ──────────────────────────────────────────────────
#
# Each returns a normalised shape so the renderer is provider-agnostic:
#   {"provider":…, "account":…, "windows": [
#       {"name":…, "used":float, "total":float, "unit":str, "resets":str|None}]}


def read_codebuddy(env_name: str = "CODEBUDDY_API_KEY") -> dict:
    """CodeBuddy bills in credits over a cycle; the answer is a REST envelope."""
    tok = _token(env_name)
    if not tok:
        raise RuntimeError(f"{env_name} not set — run `ccp auth codebuddy`")
    doc = _post_json(
        "https://www.codebuddy.ai/v2/billing/meter/get-user-resource", tok, {}
    )
    if doc.get("code") != 0:
        raise RuntimeError(f"gateway said code={doc.get('code')} msg={doc.get('msg')}")
    data = ((doc.get("data") or {}).get("Response") or {}).get("Data") or {}
    windows = []
    for acc in data.get("Accounts") or []:
        total = _num(acc.get("CycleCapacitySizePrecise") or acc.get("CapacitySize"))
        used = _num(acc.get("CycleCapacityUsedPrecise") or acc.get("CapacityUsed"))
        if total is None:
            continue
        windows.append(
            {
                "name": acc.get("PackageName") or acc.get("SubProductName") or "plan",
                "used": used or 0.0,
                "total": total,
                "unit": acc.get("OriginUnit") or "credit",
                "resets": acc.get("CycleEndTime") or None,
            }
        )
    return {"provider": "codebuddy", "account": env_name, "windows": windows}


def read_cline(env_name: str = "CLINE_API_KEY") -> dict:
    """Cline exposes identity but no published balance endpoint."""
    tok = _token(env_name)
    if not tok:
        raise RuntimeError(f"{env_name} not set")
    doc = _get_json("https://api.cline.bot/api/v1/users/me", tok)
    who = ((doc.get("data") or {}).get("email")) or "?"
    return {"provider": "cline", "account": who, "windows": [], "note": "no usage API"}


READERS = {
    "codebuddy": read_codebuddy,
    "cline": read_cline,
}


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── cache ─────────────────────────────────────────────────────────────────


def _cache_path(provider: str) -> Path:
    return CACHE_DIR / f"quota-{provider}.json"


def cached(provider: str, *, max_age: int = CACHE_TTL) -> dict | None:
    fp = _cache_path(provider)
    if not fp.is_file():
        return None
    try:
        doc = json.loads(fp.read_text())
    except Exception:
        return None
    if time.time() - doc.get("_at", 0) > max_age:
        return None
    return doc.get("value")


def store(provider: str, value: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fp = _cache_path(provider)
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(json.dumps({"_at": time.time(), "value": value}))
    os.replace(tmp, fp)


# ── rendering ─────────────────────────────────────────────────────────────


def _bar(used: float, total: float, width: int = 24) -> str:
    if total <= 0:
        return " " * width
    filled = int(round(width * min(used / total, 1.0)))
    return "█" * filled + "░" * (width - filled)


def render(results: list[dict]) -> None:
    for res in results:
        prov = res["provider"]
        if res.get("error"):
            print(f"\n\033[36m{prov}\033[0m  \033[33m{res['error']}\033[0m")
            continue
        windows = res.get("windows") or []
        print(f"\n\033[36m{prov}\033[0m  ({res.get('account','')})")
        if not windows:
            print(f"  {res.get('note') or 'nothing to report'}")
            continue
        for w in windows:
            used, total = w["used"], w["total"]
            left = total - used
            pct = (used / total * 100) if total else 0
            colour = "32" if pct < 70 else ("33" if pct < 90 else "31")
            print(f"  {w['name'][:28]:28s} {_bar(used, total)}  "
                  f"\033[{colour}m{left:,.1f} left\033[0m of {total:,.0f} {w['unit']}")
            if w.get("resets"):
                print(f"  {'':28s} resets {w['resets']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("provider", nargs="?", help="only this provider")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache")
    args = ap.parse_args()

    wanted = [args.provider] if args.provider else list(READERS)
    unknown = [p for p in wanted if p not in READERS]
    if unknown:
        print(f"no usage API known for: {', '.join(unknown)}", file=sys.stderr)
        return 1

    results = []
    for prov in wanted:
        value = None if args.refresh else cached(prov)
        if value is None:
            try:
                value = READERS[prov]()
                store(prov, value)
            except urllib.error.HTTPError as e:
                value = {"provider": prov, "error": f"HTTP {e.code} {e.reason}"}
            except Exception as e:
                value = {"provider": prov, "error": str(e)}
        results.append(value)

    if args.json:
        print(json.dumps(results, indent=2))
        return 0
    render(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
