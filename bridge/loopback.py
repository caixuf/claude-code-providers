"""Keep loopback traffic out of the HTTP proxy.

Windows hands WSL a ``no_proxy`` that uses wildcard spellings (``127.*``).
curl, urllib and httpx understand only exact hosts, domain suffixes and CIDR
blocks — none of them read ``127.*`` as "the loopback". A request for
127.0.0.1 therefore leaves the machine, the proxy cannot route a loopback
destination, and it answers ``502`` with an *empty* body: the bridge looks
dead while the process behind it is perfectly healthy.

Measured on this machine (2026-10-20): with ``no_proxy=127.*`` inherited from
Windows, every model whose endpoint is on 127.0.0.1 — the LiteLLM bridge
(:4000/:4001) and the Trae bridge (:4100) — answered ``502`` with an empty
body, while every provider on a public URL answered normally.

``~/.bashrc`` papers over this for interactive shells, but a tool cannot
depend on the shell it was launched from: a non-interactive shell, a systemd
service or a script never reads it, and a shell that was already running when
the fix landed keeps the broken value. So the bypass lives here, next to the
code that opens the sockets.
"""

from __future__ import annotations

import os
import urllib.parse

# Hosts that must never be sent to a proxy. Written as exact entries because
# that is the only spelling the HTTP stacks agree on.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

# httpx URL patterns that pin a host to a direct (proxy-less) transport.
LOOPBACK_MOUNTS = {
    "all://127.0.0.1": None,
    "all://localhost": None,
    "all://[::1]": None,
}


def normalize_no_proxy(value: str | None) -> str:
    """Return ``value`` with the loopback hosts present as exact entries.

    Existing entries are kept verbatim — the company domains and the private
    ranges that Windows maintains are none of this module's business. Only
    the loopback is added, and only when it is missing.

    A wildcard entry like ``127.*`` is deliberately left in place: it is inert
    for every stack that matters, and removing it would silently drop a
    meaning the machine's own configuration gave it.
    """
    entries = [e.strip() for e in (value or "").split(",")]
    entries = [e for e in entries if e]
    present = {e.lower() for e in entries}
    for host in LOOPBACK_HOSTS:
        if host.lower() not in present:
            entries.append(host)
    return ",".join(entries)


def ensure_loopback_bypass() -> str:
    """Make this process (and anything it spawns) skip the proxy for loopback.

    Idempotent, and safe to call from a library: it only ever *adds* the
    loopback to the bypass list. Returns the effective value.
    """
    for name in ("no_proxy", "NO_PROXY"):
        current = os.environ.get(name)
        fixed = normalize_no_proxy(current)
        if current != fixed:
            os.environ[name] = fixed
    return os.environ.get("no_proxy", "")


def is_loopback_url(url: str) -> bool:
    """True when ``url`` points at this machine.

    Used to tell "the upstream refused" apart from "the request never
    reached the upstream" — a loopback endpoint that answers 502 came from
    the proxy, not from the service.
    """
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return False
    host = host.strip("[]").lower()
    if not host:
        return False
    if host in LOOPBACK_HOSTS:
        return True
    if host.startswith("127."):
        return True
    return host == "::1"


def loopback_mounts() -> dict:
    """httpx mounts that pin the loopback to a direct transport.

    Belt and braces next to :func:`ensure_loopback_bypass`: even if something
    rewrites ``no_proxy`` after the client was built, the loopback still goes
    out directly. A plain ``httpx.AsyncHTTPTransport()`` has no proxy
    configured, so it always connects straight.
    """
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a hard dependency
        return {}
    direct = httpx.AsyncHTTPTransport()
    return {pattern: direct for pattern in LOOPBACK_MOUNTS}
