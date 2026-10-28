#!/usr/bin/env python3
"""The bridge's own credential, and what a client's credential is worth.

The bridge fronts LiteLLM on 127.0.0.1:4000. LiteLLM accepts exactly one
credential - the master key in the config it was started with - and refuses
everything else with a message that names neither the key nor the bridge:

    400 {"error": {"message": "No connected db.", "type": "no_db_connection"}}

That is what a client with a stale key gets: the virtual-key lookup wants a
database, there is none, and the error reads like the bridge is down. With no
key at all it is worse - LiteLLM's own error handler imports prisma, which is
not installed, so the request dies with a 500 before anything answers it.

Both are mistakes on the client side, and neither has to be able to stop a
call. So the bridge adapts instead of refusing: whatever credential the client
carries - right, stale, mistyped or missing - the request goes upstream with
the bridge's own key. The client decides *which model* to ask for; it does not
decide whether the call happens at all.

That is safe here because the bridge is a single-tenant proxy bound to the
loopback: there is no per-caller credential to honour, and the models it
serves are its own.

The key itself is read from the installed LiteLLM config, because that config
is what LiteLLM enforces - a hand-edited master key is the one that works.
The committed constant is only the fallback for a machine whose config cannot
be read.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# The key ccp renders into the LiteLLM config. A local placeholder, not a
# secret: it is committed, printed by `ccp doctor`, and named in the README.
MASTER_KEY = "sk-ccp-litellm-local"

# The installed LiteLLM config, in the order the paths are tried.
DEFAULT_CONFIG_PATHS = (
    Path.home() / ".config" / "litellm" / "config.yaml",
)

# `general_settings: { master_key: ... }` in the generated config.
_MASTER_KEY_RE = re.compile(r"^\s*master_key\s*:\s*['\"]?([^'\"\s#]+)", re.MULTILINE)

# The ports the local bridge answers on: the watchdog in front of LiteLLM,
# and LiteLLM itself.
BRIDGE_PORTS = ("4000", "4001")


def is_bridge_url(url: str | None) -> bool:
    """True when a base URL points at this machine's bridge.

    A native provider's URL is somebody else's server, and its credential is
    the vendor's own - that one is never the bridge's key.
    """
    if not url:
        return False
    text = url.strip().lower()
    if not any(f":{port}" in text for port in BRIDGE_PORTS):
        return False
    return any(host in text for host in ("127.0.0.1", "localhost", "::1", "[::1]"))


def config_path(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """The LiteLLM config to read the master key from, if there is one."""
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    env = os.environ.get("LITELLM_CONFIG") or os.environ.get("CCP_LITELLM_CONFIG")
    if env:
        p = Path(env).expanduser()
        return p if p.is_file() else None
    for p in DEFAULT_CONFIG_PATHS:
        if p.is_file():
            return p
    return None


def load_master_key(explicit: str | os.PathLike[str] | None = None) -> tuple[str, str]:
    """Return (key, source): the credential LiteLLM enforces, and where it came from.

    The installed config is the truth - it is the document LiteLLM was
    started with. The constant is the fallback for a machine whose config
    cannot be read.
    """
    env = os.environ.get("CCP_MASTER_KEY")
    if env:
        return env, "CCP_MASTER_KEY"
    p = config_path(explicit)
    if p is not None:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            text = ""
        m = _MASTER_KEY_RE.search(text)
        if m:
            return m.group(1), str(p)
    return MASTER_KEY, "built-in"


def _token(value: str | None) -> str:
    v = (value or "").strip()
    if v.lower().startswith("bearer "):
        return v[7:].strip()
    return v


def normalize_auth(
    headers: dict[str, str], master_key: str | None = None
) -> tuple[dict[str, str], str]:
    """Replace the client's credential with the bridge's.

    Returns (headers, note), where note is:

    * ``""`` - the client already carried the right credential;
    * ``"replaced"`` - it carried a different one (stale, mistyped, another
      provider's key);
    * ``"added"`` - it carried none at all.
    """
    key = master_key or MASTER_KEY
    out: dict[str, str] = {}
    auth: str | None = None
    xapi: str | None = None
    for k, v in headers.items():
        lk = k.lower()
        if lk == "authorization":
            auth = v
            continue
        if lk == "x-api-key":
            xapi = v
            continue
        out[k] = v
    client = _token(auth if auth is not None else xapi)
    if auth is not None:
        out["authorization"] = f"Bearer {key}"
        if xapi is not None:
            out["x-api-key"] = key
        return out, ("" if client == key else "replaced")
    if xapi is not None:
        out["x-api-key"] = key
        return out, ("" if client == key else "replaced")
    out["authorization"] = f"Bearer {key}"
    return out, "added"


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=None, help="the LiteLLM config to read")
    ap.add_argument(
        "--print-master-key",
        action="store_true",
        help="print the credential LiteLLM enforces, and nothing else",
    )
    args = ap.parse_args()
    key, source = load_master_key(args.config)
    if args.print_master_key:
        print(key)
    else:
        print(json.dumps({"master_key": key, "source": source}, indent=2))
