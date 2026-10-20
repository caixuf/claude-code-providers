"""The loopback must never be handed to the HTTP proxy.

Windows hands WSL a ``no_proxy`` written with wildcards (``127.*``). No HTTP
stack understands that spelling — they read exact hosts, domain suffixes and
CIDR blocks — so a request for 127.0.0.1 leaves the machine, the proxy
answers for a destination it cannot route, and it replies 502 with an empty
body. The provider then looks dead while the process behind it is healthy.

These tests pin the bypass and the reporting that goes with it.
"""

import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bridge"))

from loopback import (  # noqa: E402
    LOOPBACK_HOSTS,
    ensure_loopback_bypass,
    is_loopback_url,
    loopback_mounts,
    normalize_no_proxy,
)


def test_normalize_adds_the_loopback_as_exact_entries():
    # The wildcard spelling Windows hands down is inert for every HTTP
    # stack, so the exact host has to be there as well.
    out = normalize_no_proxy("127.*,.immotors.com")
    assert "127.0.0.1" in out.split(",")
    assert "::1" in out.split(",")
    assert "localhost" in out.split(",")
    # The machine's own entries survive untouched.
    assert ".immotors.com" in out.split(",")
    assert "127.*" in out.split(",")


def test_normalize_keeps_what_is_already_there():
    out = normalize_no_proxy("127.0.0.1,localhost,::1")
    assert out.split(",").count("127.0.0.1") == 1
    assert out == "127.0.0.1,localhost,::1"


def test_normalize_of_nothing_still_names_the_loopback():
    out = normalize_no_proxy(None)
    for host in LOOPBACK_HOSTS:
        assert host in out.split(",")


def test_ensure_bypass_fixes_both_spellings(monkeypatch):
    monkeypatch.setenv("no_proxy", "127.*")
    monkeypatch.setenv("NO_PROXY", "127.*")
    ensure_loopback_bypass()
    for name in ("no_proxy", "NO_PROXY"):
        entries = os.environ[name].split(",")
        assert "127.0.0.1" in entries
        assert "::1" in entries
        assert "localhost" in entries


def test_ensure_bypass_is_idempotent(monkeypatch):
    monkeypatch.setenv("no_proxy", "127.*")
    monkeypatch.setenv("NO_PROXY", "127.*")
    first = ensure_loopback_bypass()
    second = ensure_loopback_bypass()
    assert first == second


def test_is_loopback_url():
    assert is_loopback_url("http://127.0.0.1:4000/v1/messages")
    assert is_loopback_url("http://localhost:4100/v1/messages")
    assert is_loopback_url("http://[::1]:4000/v1/messages")
    assert is_loopback_url("http://127.0.0.9:4000/v1/messages")
    assert not is_loopback_url("https://api.deepseek.com/anthropic")
    assert not is_loopback_url("https://amr-link.open-design.ai")


def test_loopback_mounts_pin_the_loopback_to_a_direct_transport():
    mounts = loopback_mounts()
    assert set(mounts) == {"all://127.0.0.1", "all://localhost", "all://[::1]"}
    # A transport with no proxy configured connects straight.
    for transport in mounts.values():
        assert transport._pool._proxy is None


def test_a_loopback_request_bypasses_the_proxy(monkeypatch):
    """The end-to-end promise: a loopback URL never goes to the proxy.

    httpx reads ``no_proxy`` when it builds its transports, so a wildcard
    entry alone leaves the loopback proxied. The mounts are the backstop,
    and the environment is the first line of defence.
    """
    import httpx

    monkeypatch.setenv("http_proxy", "http://127.0.0.1:7897")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7897")
    monkeypatch.setenv("no_proxy", "127.*")
    monkeypatch.setenv("NO_PROXY", "127.*")

    ensure_loopback_bypass()
    client = httpx.AsyncClient(mounts=loopback_mounts())
    # No socket was opened, so there is nothing to close: the pool is lazy.
    loopback = client._transport_for_url(httpx.URL("http://127.0.0.1:4000/v1/messages"))
    assert loopback._pool._proxy is None
    # A public URL keeps the proxy, where it is wanted.
    public = client._transport_for_url(httpx.URL("https://api.deepseek.com/anthropic"))
    assert public is not loopback
    assert type(public._pool).__name__ == "AsyncHTTPProxy"
