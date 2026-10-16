#!/usr/bin/env python3
"""Import the TraeCLI personal access token into ccp's secrets file.

TraeCLI does not issue an API key. What a logged-in TraeCLI has is a personal
access token, which it writes to ``~/.bashrc`` on first login:

    export TRAECLI_PERSONAL_ACCESS_TOKEN="trae-lt-..."

``trae_bridge.py`` exchanges that PAT for a short-lived JWT at startup, so the
PAT is the only durable secret. This command copies it into
``~/.config/litellm/proxy.env`` — the same file every other provider's key
lives in, and the one the systemd units load via ``EnvironmentFile`` — so the
bridge works without depending on an interactive shell.

Rules this follows (same as bridge/archived/codebuddy_auth.py)
----------------------------------------------------
* **Read, never refresh.** The PAT is long-lived and not rotated by ccp; the
  bridge exchanges it for a JWT itself. There is nothing here that could
  invalidate the token TraeCLI is using.
* **A file we could not read is an error, never an empty file.** proxy.env
  holds every provider's key; writing over content we failed to parse would
  silently drop the others.
* **Write durably and atomically** (temp file + os.replace in the same dir), so
  a crash mid-write cannot leave a truncated — i.e. credential-less — file.
* **Never print the token** unless explicitly asked.

Usage:
  python3 trae_auth.py                # import, then verify the subscription answers
  python3 trae_auth.py --status       # is proxy.env in sync with ~/.bashrc?
  python3 trae_auth.py --no-verify    # import without touching the network
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

TOKEN_ENV = "TRAECLI_PERSONAL_ACCESS_TOKEN"
PROXY_ENV = Path(os.path.expanduser("~/.config/litellm/proxy.env"))
SHELL_RC = Path(os.path.expanduser("~/.bashrc"))
ASSIGN = re.compile(r'^\s*(?:export\s+)?%s\s*=\s*["\']?([^"\'\s#]+)["\']?'
                    % re.escape(TOKEN_ENV))


def read_token_from_rc(path: Path = SHELL_RC) -> str | None:
    """Scrape the token out of the shell rc file TraeCLI writes."""
    if not path.is_file():
        return None
    try:
        for line in path.read_text(errors="replace").splitlines():
            m = ASSIGN.match(line)
            if m:
                return m.group(1)
    except OSError as e:
        print(f"! cannot read {path}: {e}", file=sys.stderr)
    return None


def env_token() -> str | None:
    v = os.environ.get(TOKEN_ENV)
    return v or None


def stored_token() -> str | None:
    if not PROXY_ENV.is_file():
        return None
    try:
        for line in PROXY_ENV.read_text(errors="replace").splitlines():
            m = ASSIGN.match(line)
            if m:
                return m.group(1)
    except OSError:
        return None
    return None


def write_token(token: str) -> None:
    """Replace/append the token line, keeping every other entry byte-identical."""
    if PROXY_ENV.is_file():
        try:
            lines = PROXY_ENV.read_text().splitlines()
        except OSError as e:
            raise SystemExit(f"cannot read {PROXY_ENV}: {e} (refusing to overwrite)")
    else:
        lines = []

    replaced = False
    out = []
    for line in lines:
        if ASSIGN.match(line):
            if not replaced:
                out.append("%s=%s" % (TOKEN_ENV, token))
                replaced = True
            # drop any duplicate token lines: the last one would win anyway
            continue
        out.append(line)
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append("%s=%s" % (TOKEN_ENV, token))

    PROXY_ENV.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(PROXY_ENV.parent), prefix=".proxy.env.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(out) + "\n")
        os.chmod(tmp, 0o600)  # proxy.env holds every provider's key
        os.replace(tmp, PROXY_ENV)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def verify(token: str) -> bool:
    """Prove the subscription actually answers — an unexpired PAT can still be
    rejected, and a 200 from the token exchange alone does not mean the models
    are reachable."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import json
    import urllib.request

    try:
        import trae_bridge
    except Exception as e:
        print(f"! cannot import trae_bridge: {e}", file=sys.stderr)
        return False
    try:
        auth = trae_bridge.TraeAuth(pat=token)
        auth.jwt()
        cat = auth.catalogue()
    except SystemExit as e:
        print("\033[31m! %s\033[0m" % e, file=sys.stderr)
        return False
    except Exception as e:
        print(f"\033[31m! subscription check failed: {e}\033[0m", file=sys.stderr)
        return False
    print("\033[32m✔ subscription verified\033[0m — %d models available" % len(cat))
    return bool(cat)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true",
                    help="report whether proxy.env matches the shell rc file")
    ap.add_argument("--no-verify", action="store_true", help="import without network")
    ap.add_argument("--print-token", action="store_true")
    args = ap.parse_args()

    if args.print_token:
        t = env_token() or read_token_from_rc() or stored_token()
        if not t:
            sys.exit("no %s found" % TOKEN_ENV)
        print(t)
        return

    source = env_token() or read_token_from_rc()
    if args.status:
        stored = stored_token()
        if not source:
            print("no %s in the environment or %s" % (TOKEN_ENV, SHELL_RC))
            return sys.exit(1 if not stored else 0)
        if not stored:
            print("proxy.env has no %s — run: ccp auth trae" % TOKEN_ENV)
            return 1
        if stored != source:
            print("proxy.env is STALE — run: ccp auth trae")
            return 1
        print("proxy.env is in sync (%s)" % TOKEN_ENV)
        return 0

    if not source:
        sys.exit(
            "No %s found.\n"
            "Log in with TraeCLI first (it writes the token to %s), or export it:\n"
            "    export %s=trae-lt-..." % (TOKEN_ENV, SHELL_RC, TOKEN_ENV))

    if source != stored_token():
        write_token(source)
        print("wrote %s to %s" % (TOKEN_ENV, PROXY_ENV))
    else:
        print("%s already current in %s" % (TOKEN_ENV, PROXY_ENV))

    if not args.no_verify and not verify(source):
        return 1

    print("")
    print("\033[32m✔ Trae credentials ready!\033[0m")
    print("Start the bridge:  ccp bridge start trae")
    print("Switch profile:    ccp trae")


if __name__ == "__main__":
    main()