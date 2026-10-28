#!/usr/bin/env python3
"""The credential a native provider's profile should carry.

The rendered template ships a placeholder (``YOUR_OPENDESIGN_API_KEY``): the
repository must not carry a secret. But the *installed* profile is what Claude
Code reads, so a profile that is installed from a template needs the real
value — otherwise the provider answers 401 and the profile reads as broken.

The real value lives in ``~/.config/litellm/proxy.env`` under the provider's
``auth_env`` — the same name the registry records and the same file the
bridge services load. So the credential has one home, and this resolves it.

Usage:
    profile_credential.py <provider>      # prints the token, or nothing
"""
from __future__ import annotations

import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
REGISTRY = REPO / "registry" / "models.yaml"
PROXY_ENV = pathlib.Path(os.path.expanduser("~/.config/litellm/proxy.env"))


def auth_env_for(provider: str) -> str | None:
    """The env var name that holds this provider's credential."""
    try:
        import yaml
    except ImportError:  # pragma: no cover - yaml is present in the venv
        return None
    try:
        doc = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    except Exception:
        return None
    for p in doc.get("providers") or []:
        if p.get("name") == provider:
            return p.get("auth_env")
    return None


def token_from_proxy_env(env_name: str | None) -> str:
    if not env_name or not PROXY_ENV.is_file():
        return ""
    for line in PROXY_ENV.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith(env_name + "="):
            return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: profile_credential.py <provider>", file=sys.stderr)
        return 2
    token = token_from_proxy_env(auth_env_for(argv[1]))
    if not token:
        return 1
    print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
