#!/usr/bin/env python3
"""Import the local CodeBuddy Code login token into the bridge's secrets file.

CodeBuddy's gateway (https://www.codebuddy.ai, or https://copilot.tencent.com
for CN accounts) is not an API-key service: it authenticates the *session* the
CodeBuddy Code CLI already holds. That session lives on disk as plain JSON:

    ~/.local/share/CodeBuddyExtension/Data/Public/auth/<auth-id>.info
    {"account": {"uid": ...}, "auth": {"accessToken": "eyJ...", "expiresAt": ...}}

so `ccp auth codebuddy` reads it and appends

    CODEBUDDY_API_KEY=<accessToken>

to ~/.config/litellm/proxy.env, which the generated LiteLLM config references as
``os.environ/CODEBUDDY_API_KEY`` and the systemd unit loads via EnvironmentFile.

The token is a long-lived (≈1 year) OAuth access token; re-run this command
after the CLI refreshes or you re-login.

Usage:
  python3 codebuddy_auth.py                # import, no network
  python3 codebuddy_auth.py --verify       # import, then ping the gateway
  python3 codebuddy_auth.py --print-token  # print the raw token (CI/pipes)
  python3 codebuddy_auth.py --auth-file PATH
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import ssl
import sys
import urllib.request
from pathlib import Path

AUTH_ENV = "CODEBUDDY_API_KEY"
PROXY_ENV = Path(os.path.expanduser("~/.config/litellm/proxy.env"))
GATEWAY = "https://www.codebuddy.ai/v2/chat/completions"

# Where CodeBuddy Code / the CodeBuddy IDE keep the session, per platform.
# WSL needs the /mnt/c row because the Windows client writes its own copy.
AUTH_GLOBS = [
    "~/.local/share/CodeBuddyExtension/Data/Public/auth/*.info",
    "~/.local/share/CodeBuddyCode/Data/Public/auth/*.info",
    "~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/*.info",
    "~/Library/Application Support/CodeBuddyCode/Data/Public/auth/*.info",
    "/mnt/c/Users/*/AppData/Local/CodeBuddyExtension/Data/Public/auth/*.info",
    "/mnt/c/Users/*/AppData/Local/CodeBuddyCode/Data/Public/auth/*.info",
    "~/.codebuddy/auth/*.info",
]


def candidate_files(explicit: str | None) -> list[Path]:
    if explicit:
        return [Path(os.path.expanduser(explicit))]
    env = os.environ.get("CODEBUDDY_AUTH_FILE")
    if env:
        return [Path(os.path.expanduser(env))]
    found: list[Path] = []
    for pat in AUTH_GLOBS:
        found.extend(Path(p) for p in glob.glob(os.path.expanduser(pat)))
    # newest first: rotations leave older copies beside the live file
    return sorted(set(found), key=lambda p: p.stat().st_mtime, reverse=True)


def load_session(path: Path) -> dict | None:
    try:
        doc = json.loads(path.read_text())
    except Exception:
        return None
    if not isinstance(doc, dict):
        return None
    auth = doc.get("auth")
    if not isinstance(auth, dict):
        return None
    token = auth.get("accessToken")
    if isinstance(token, dict):
        raise SystemExit(
            f"{path}\n"
            "  This file was written by the CodeBuddy *desktop* client (>= 5.6.0),\n"
            "  which seals the token in an AES-GCM envelope only that binary can open.\n"
            "  Log in with the CodeBuddy Code CLI instead (`codebuddy`), which stores\n"
            "  the session in the clear, then re-run `ccp auth codebuddy`."
        )
    if not isinstance(token, str) or not token:
        return None
    return {
        "path": path,
        "token": token,
        "uid": (doc.get("account") or {}).get("uid") or "",
        "nickname": (doc.get("account") or {}).get("nickname") or "",
        "domain": auth.get("domain") or "",
        "expires_at": auth.get("expiresAt"),
    }


def find_session(explicit: str | None) -> dict:
    tried = []
    for path in candidate_files(explicit):
        tried.append(str(path))
        if not path.is_file():
            continue
        sess = load_session(path)
        if sess:
            return sess
    where = "\n  ".join(tried) if tried else "(no candidate paths matched)"
    raise SystemExit(
        "No CodeBuddy login found. Looked at:\n  " + where + "\n"
        "Log in first by running `codebuddy` once, then re-run this command."
    )


def write_proxy_env(token: str, path: Path = PROXY_ENV) -> bool:
    """Set CODEBUDDY_API_KEY in proxy.env, preserving every other line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if path.is_file():
        lines = [
            ln
            for ln in path.read_text().splitlines()
            if not ln.strip().startswith(AUTH_ENV + "=")
        ]
    lines.append(f"{AUTH_ENV}={token}")
    path.write_text("\n".join(lines) + "\n")
    try:
        path.chmod(0o600)
    except Exception:
        pass
    return True


def verify(token: str, model: str = "deepseek-v4.1-flash") -> tuple[bool, str]:
    """Stream one tiny completion. Returns (ok, detail).

    The gateway speaks ONLY streaming (a non-stream request is rejected with
    code 11101), and it requires the first message to be a system turn, so this
    probe sends exactly what the bridge sends.
    """
    body = json.dumps(
        {
            "model": model,
            "stream": True,
            "max_tokens": 16,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "reply with the single word: pong"},
            ],
        }
    ).encode()
    req = urllib.request.Request(
        GATEWAY,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
            head = resp.read(400).decode("utf-8", "replace")
    except Exception as e:  # pragma: no cover - network dependent
        return False, f"request failed: {e}"
    if head.lstrip().startswith("{"):
        try:
            err = json.loads(head)
        except Exception:
            err = {}
        return False, f"gateway rejected the token: code={err.get('code')} msg={err.get('msg')}"
    if "data:" not in head:
        return False, f"unexpected response: {head[:160]}"
    return True, f"streaming completion OK (model={model})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--auth-file", default=None, help="explicit CodeBuddy session file")
    ap.add_argument("--verify", action="store_true", help="ping the gateway after importing")
    ap.add_argument("--verify-model", default="deepseek-v4.1-flash")
    ap.add_argument("--print-token", action="store_true", help="print the raw token")
    ap.add_argument("--proxy-env", default=str(PROXY_ENV))
    args = ap.parse_args()

    sess = find_session(args.auth_file)
    if args.print_token:
        print(sess["token"])
        return 0

    write_proxy_env(sess["token"], Path(os.path.expanduser(args.proxy_env)))

    masked = sess["token"][:12] + "..." + sess["token"][-6:]
    print(f"  Source:  {sess['path']}")
    if sess["nickname"]:
        print(f"  Account: {sess['nickname']}" + (f" ({sess['uid']})" if sess["uid"] else ""))
    if sess["domain"]:
        print(f"  Domain:  {sess['domain']}")
    exp = sess["expires_at"]
    if isinstance(exp, (int, float)) and exp > 0:
        import datetime

        when = datetime.datetime.fromtimestamp(exp / 1000).strftime("%Y-%m-%d %H:%M")
        print(f"  Expires: {when}")
    print(f"  Token:   {masked}")
    print(f"  Wrote {AUTH_ENV} to {os.path.expanduser(args.proxy_env)}")

    if args.verify:
        ok, detail = verify(sess["token"], args.verify_model)
        print(("  Verify:  OK — " if ok else "  Verify:  FAILED — ") + detail)
        if not ok:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
