#!/usr/bin/env python3
"""Import the local CodeBuddy Code login into the bridge's secrets file.

CodeBuddy's gateway (https://www.codebuddy.ai for international accounts,
https://copilot.tencent.com for CN ones) is not an API-key service: it
authenticates the *session* the CodeBuddy Code CLI already holds. That session
lives on disk as plain JSON:

    ~/.local/share/CodeBuddyExtension/Data/Public/auth/<auth-id>.info
    {"account": {"uid": ...}, "auth": {"accessToken": "eyJ...", "refreshToken": ...,
                                       "domain": "www.codebuddy.cn", "expiresAt": ...}}

so `ccp auth codebuddy` reads it and writes

    CODEBUDDY_API_KEY=<accessToken>

into ~/.config/litellm/proxy.env, which the generated LiteLLM config references
as ``os.environ/CODEBUDDY_API_KEY`` and the systemd unit loads via EnvironmentFile.
Which of the two gateways to talk to comes from ``auth.domain`` — see
``api_base()``.

## The rules this file follows

They come from studying how magpie (yetone/magpie) manages agent credentials —
its ``docs/subsystems/providers-accounts.md`` — and they are the parts of that
design that apply to a tool which does *not* own the account:

* **Read, never refresh.** The CLI is the one holder of the refresh token, and
  CodeBuddy rotates it on every refresh: if ccp refreshed, it would rotate the
  token out from under the CLI and sign it out. So this command only ever reads.
  When the access token lapses, the fix is to run `codebuddy` once — it renews
  its own session and writes it back where we read it.
* **A credential file that cannot be read is an error, never an empty file.**
  proxy.env holds every provider's key; saving over content we failed to read
  would silently drop the others.
* **Write durably and atomically.** flushed temp file + ``os.replace`` in the
  same directory. magpie learned this the hard way: a Windows crash left its
  state files at full length with every byte zero. A zeroed proxy.env would
  take down every provider at once, not just this one.
* **Secrets are never resurrected from a backup.** magpie refuses to read its
  gateway-key store from a ``.bak`` ("a key revoked in the last write would come
  back"). ccp therefore keeps no backup of proxy.env — and the config backups
  that ``ccp bridge deploy`` does make are safe to keep, because the generated
  config references ``os.environ/…`` and holds no key.
* **Refuse a credential that cannot renew itself.** magpie refuses a sign-in
  with an access token and no refresh token, since it would die within days. A
  CodeBuddy session without a refresh token is reported, not silently used.

Usage:
  python3 codebuddy_auth.py                # import, then verify
  python3 codebuddy_auth.py --status       # is proxy.env in sync? is it valid?
  python3 codebuddy_auth.py --if-stale     # re-import only if the CLI rotated
  python3 codebuddy_auth.py --no-verify    # import without hitting the network
  python3 codebuddy_auth.py --print-token  # print the raw token (CI/pipes)
  python3 codebuddy_auth.py --auth-file PATH
"""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import ssl
import sys
import tempfile
import urllib.request
from pathlib import Path

AUTH_ENV = "CODEBUDDY_API_KEY"
PROXY_ENV = Path(os.path.expanduser("~/.config/litellm/proxy.env"))

# CodeBuddy runs two independent gateways, and a session is valid at exactly the
# one it was issued by: the international site (www.codebuddy.ai) and the CN site
# (copilot.tencent.com, behind www.codebuddy.cn / tencent.sso.codebuddy.cn). A
# token from one is rejected by the other with a bare `401 invalid_token` — no
# hint that the host is what is wrong. The login records which site it signed
# into in `auth.domain`, so the host is derived from the session rather than
# assumed; CODEBUDDY_API_BASE overrides for a non-standard deployment.
INTL_API_BASE = "https://www.codebuddy.ai/v2"
CN_API_BASE = "https://copilot.tencent.com/v2"
API_BASE_ENV = "CODEBUDDY_API_BASE"

_DOMAIN_API_BASES = {
    "www.codebuddy.ai": INTL_API_BASE,
    "www.codebuddy.cn": CN_API_BASE,
    "copilot.tencent.com": CN_API_BASE,
    "tencent.sso.codebuddy.cn": CN_API_BASE,
}

# Kept for callers that predate host resolution (and the tests): the INTL chat
# endpoint. `chat_url()` is what actually picks the right host.
GATEWAY = INTL_API_BASE + "/chat/completions"


def api_base(session: dict | None = None) -> str:
    """The gateway base this login is actually valid at."""
    override = os.environ.get(API_BASE_ENV)
    if override:
        return override.rstrip("/")
    domain = ((session or {}).get("domain") or "").strip().lower()
    return _DOMAIN_API_BASES.get(domain, INTL_API_BASE)


def chat_url(session: dict | None = None) -> str:
    return api_base(session) + "/chat/completions"


# Warn this long before the access token lapses, so a session that is about to
# die is noticed at a bridge restart rather than in the middle of a turn.
EXPIRY_WARN = datetime.timedelta(days=3)

# Where CodeBuddy Code / the CodeBuddy IDE keep the session, per platform.
# WSL needs the /mnt/c rows because the Windows client writes its own copy.
AUTH_GLOBS = [
    "~/.local/share/CodeBuddyExtension/Data/Public/auth/*.info",
    "~/.local/share/CodeBuddyCode/Data/Public/auth/*.info",
    "~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/*.info",
    "~/Library/Application Support/CodeBuddyCode/Data/Public/auth/*.info",
    "/mnt/c/Users/*/AppData/Local/CodeBuddyExtension/Data/Public/auth/*.info",
    "/mnt/c/Users/*/AppData/Local/CodeBuddyCode/Data/Public/auth/*.info",
    "~/.codebuddy/auth/*.info",
]


class CredentialError(Exception):
    """A credential or its store is unusable, and we refuse to guess."""


# ── the secrets file ──────────────────────────────────────────────────────


def read_env_value(path: Path, key: str = AUTH_ENV) -> str | None:
    """Value of ``key`` in an env file, or None. A missing file reads as None."""
    if not path.is_file():
        return None
    for line in path.read_text().splitlines():
        if line.strip().startswith(key + "="):
            return line.split("=", 1)[1].strip().strip("'\"")
    return None


def _durable_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically and durably.

    Temp file in the same directory, flushed *and* fsynced, then ``os.replace``.
    Without the fsync a crash can leave the target at full length with every
    byte zero — which for proxy.env means every provider loses its key at once.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # Best effort: make the rename itself durable too.
    try:
        dfd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def write_env_value(path: Path, value: str, key: str = AUTH_ENV) -> bool:
    """Set ``key=value`` in an env file, keeping every other line.

    Returns True when the file changed. Raises CredentialError if the file
    exists but cannot be read: it carries the other providers' keys, and
    rewriting it from an empty read would drop them.
    """
    lines: list[str] = []
    if path.exists():
        try:
            current = path.read_text()
        except OSError as e:
            raise CredentialError(
                f"{path} exists but cannot be read ({e}); refusing to rewrite it, "
                f"since it holds the other providers' keys"
            ) from e
        lines = [ln for ln in current.splitlines() if not ln.strip().startswith(key + "=")]
    text = "\n".join(lines + [f"{key}={value}"]) + "\n"
    if path.is_file() and path.read_text() == text:
        return False
    _durable_write(path, text)
    return True


# ── the CodeBuddy session ─────────────────────────────────────────────────


def classify_auth_file(path: Path) -> tuple[str, str]:
    """``(status, detail)`` for one candidate file, so failures are specific.

    magpie reports a file it could not use as a failed row with its place,
    never by skipping it silently; the same applies here, where the difference
    between "you are not logged in" and "your client seals the token" decides
    what the user should do next.
    """
    if not path.exists():
        return "missing", "no such file"
    try:
        raw = path.read_text()
    except OSError as e:
        return "unreadable", str(e)
    try:
        doc = json.loads(raw)
    except Exception:
        return "badjson", "not JSON (a partially written rotation?)"
    if not isinstance(doc, dict):
        return "badjson", "JSON is not an object"
    auth = doc.get("auth")
    if not isinstance(auth, dict):
        return "no-auth", "no 'auth' object"
    token = auth.get("accessToken")
    if isinstance(token, dict):
        return "encrypted", (
            "written by the CodeBuddy desktop client (>= 5.6.0), which seals the token "
            "in an AES-GCM envelope only that binary can open"
        )
    if not isinstance(token, str) or not token:
        return "no-token", "no accessToken string"
    return "ok", ""


def session_from_file(path: Path) -> dict:
    doc = json.loads(path.read_text())
    auth = doc["auth"]
    return {
        "path": path,
        "token": auth["accessToken"],
        "refresh_token": auth.get("refreshToken"),
        "uid": (doc.get("account") or {}).get("uid") or "",
        "nickname": (doc.get("account") or {}).get("nickname") or "",
        "domain": auth.get("domain") or "",
        "expires_at": auth.get("expiresAt"),
    }


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


def find_session(explicit: str | None) -> tuple[dict | None, list[tuple[Path, str, str]]]:
    """Newest usable session, plus ``(path, status, detail)`` for every candidate."""
    checked: list[tuple[Path, str, str]] = []
    for path in candidate_files(explicit):
        status, detail = classify_auth_file(path)
        checked.append((path, status, detail))
        if status == "ok":
            return session_from_file(path), checked
    return None, checked


def describe_no_session(checked: list[tuple[Path, str, str]]) -> str:
    lines = ["No usable CodeBuddy login found."]
    if not checked:
        lines.append("  No candidate path exists on this machine.")
    else:
        lines.append("  Checked, newest first:")
        for path, status, detail in checked:
            lines.append(f"    [{status:10s}] {path}" + (f" — {detail}" if detail else ""))
    lines.append("  Log in by running `codebuddy` once, then re-run this command.")
    return "\n".join(lines)


# ── expiry, without ever refreshing ───────────────────────────────────────


def expiry_info(sess: dict) -> tuple[datetime.datetime | None, str]:
    exp = sess.get("expires_at")
    if not isinstance(exp, (int, float)) or exp <= 0:
        return None, "unknown"
    when = datetime.datetime.fromtimestamp(exp / 1000)
    delta = when - datetime.datetime.now()
    if delta.total_seconds() <= 0:
        return when, "EXPIRED"
    days = delta.days
    span = f"in {days} day{'s' if days != 1 else ''}" if days >= 1 else "in under a day"
    return when, f"{when:%Y-%m-%d %H:%M} ({span})"


def verify(token: str, model: str = "deepseek-v4.1-flash",
           session: dict | None = None) -> tuple[bool, str]:
    """Stream one tiny completion. Returns (ok, detail).

    The gateway speaks ONLY streaming (a non-stream request is rejected with
    code 11101), and it requires the first message to be a system turn, so this
    probe sends exactly what the bridge sends. ``session`` selects the host the
    login is valid at — probing a CN token at the INTL edge is the 401 this
    would otherwise report as a broken login.
    """
    url = chat_url(session)
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
        url,
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


# ── commands ──────────────────────────────────────────────────────────────


def _import(args, *, quiet: bool) -> int:
    """Shared body of the plain import and --if-stale."""
    env_path = Path(os.path.expanduser(args.proxy_env))
    sess, checked = find_session(args.auth_file)
    if sess is None:
        if quiet and read_env_value(env_path) is None:
            return 0  # CodeBuddy simply is not in use here
        print(describe_no_session(checked), file=sys.stderr)
        return 1

    _, expiry = expiry_info(sess)
    if expiry == "EXPIRED":
        print(
            f"  The CodeBuddy login in {sess['path']} has EXPIRED.\n"
            "  ccp never refreshes it: the CLI holds the rotating refresh token, and "
            "refreshing here would sign your CLI out.\n"
            "  Run `codebuddy` once — it renews its own session — then re-run this.",
            file=sys.stderr,
        )
        return 1

    existing = read_env_value(env_path)
    if quiet and existing == sess["token"]:
        return 0

    write_env_value(env_path, sess["token"])

    if not quiet:
        masked = sess["token"][:12] + "..." + sess["token"][-6:]
        print(f"  Source:  {sess['path']}")
        if sess["nickname"]:
            print(f"  Account: {sess['nickname']}" + (f" ({sess['uid']})" if sess["uid"] else ""))
        if sess["domain"]:
            print(f"  Domain:  {sess['domain']}")
        print(f"  Expires: {expiry}")
        print(f"  Token:   {masked}")
        if not sess.get("refresh_token"):
            print(
                "  ⚠ No refreshToken in this login: it cannot renew itself, and a "
                "re-login will be needed once it lapses."
            )
        print(f"  Wrote {AUTH_ENV} to {env_path}")
    else:
        why = "adopted the CLI's rotated token" if existing else "imported the CLI login"
        print(f"  CodeBuddy: {why} (from {sess['path'].name})")

    when, _ = expiry_info(sess)
    if when is not None and when - datetime.datetime.now() < EXPIRY_WARN and not quiet:
        print(f"  ⚠ The access token lapses {when:%Y-%m-%d %H:%M}; run `codebuddy` before then.")

    if args.verify and not args.no_verify:
        ok, detail = verify(sess["token"], args.verify_model, session=sess)
        print(("  Verify:  OK — " if ok else "  Verify:  FAILED — ") + detail)
        if not ok:
            return 1
    return 0


def cmd_status(args) -> int:
    env_path = Path(os.path.expanduser(args.proxy_env))
    stored = read_env_value(env_path)
    sess, checked = find_session(args.auth_file)
    if sess is None:
        print(describe_no_session(checked), file=sys.stderr)
        return 1

    _, expiry = expiry_info(sess)
    print(f"  Source:   {sess['path']}")
    if sess["nickname"]:
        print(f"  Account:  {sess['nickname']}" + (f" ({sess['uid']})" if sess["uid"] else ""))
    print(f"  Expires:  {expiry}")
    print(f"  Refresh:  {'present' if sess.get('refresh_token') else 'MISSING (cannot renew itself)'}")

    src_mtime = sess["path"].stat().st_mtime
    env_mtime = env_path.stat().st_mtime if env_path.is_file() else 0.0
    if stored is None:
        print(f"  proxy.env: no {AUTH_ENV} — run `ccp auth codebuddy`")
        state = "missing"
    elif stored == sess["token"]:
        when = "the CLI file has not changed since" if src_mtime <= env_mtime else "re-imported below"
        print(f"  proxy.env: in sync with the CLI ({when})")
        state = "synced"
    else:
        print("  proxy.env: OUT OF SYNC — the CLI holds a different token (it rotated one)")
        print("             run `ccp auth codebuddy`, or `ccp bridge restart`, to adopt it")
        state = "stale"

    if expiry == "EXPIRED":
        print("  → the token has lapsed; run `codebuddy` once, then `ccp auth codebuddy`")
    return 0 if state == "synced" and expiry != "EXPIRED" else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--auth-file", default=None, help="explicit CodeBuddy session file")
    ap.add_argument("--proxy-env", default=str(PROXY_ENV))
    ap.add_argument("--verify", action="store_true", help="ping the gateway after importing")
    ap.add_argument("--no-verify", action="store_true", help="never hit the network")
    ap.add_argument("--verify-model", default="deepseek-v4.1-flash")
    ap.add_argument("--status", action="store_true", help="report sync/expiry, import nothing")
    ap.add_argument(
        "--if-stale",
        action="store_true",
        help="re-import only when the CLI rotated the token (used by `ccp bridge restart`)",
    )
    ap.add_argument("--print-token", action="store_true", help="print the raw token")
    args = ap.parse_args()

    if args.print_token:
        sess, _ = find_session(args.auth_file)
        if sess is None:
            print("no usable CodeBuddy login found", file=sys.stderr)
            return 1
        print(sess["token"])
        return 0

    try:
        if args.status:
            return cmd_status(args)
        if args.if_stale:
            return _import(args, quiet=True)
        return _import(args, quiet=False)
    except CredentialError as e:
        print(f"  ✘ {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
