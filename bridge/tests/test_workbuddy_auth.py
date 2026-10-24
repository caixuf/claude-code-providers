"""Guard rails for the credential store.

These pin the invariants ccp adopted from magpie's credential design
(``docs/subsystems/providers-accounts.md``): a secrets file we cannot read is
never rewritten, writes are atomic and durable, an unusable login says *why*,
and ccp never refreshes a token another tool owns.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bridge"))

import workbuddy_auth as cb  # noqa: E402


def _session_file(path: Path, *, token="tok-1", refresh="ref-1", expires_in_days=300) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "account": {"uid": "u-1", "nickname": "me@example.com"},
                "auth": {
                    "accessToken": token,
                    "refreshToken": refresh,
                    "domain": "www.codebuddy.ai",
                    "expiresAt": int((time.time() + expires_in_days * 86400) * 1000),
                },
            }
        )
    )
    return path


def _args(path: Path, env: Path, **over) -> argparse.Namespace:
    base = dict(
        auth_file=str(path),
        proxy_env=str(env),
        verify=False,
        no_verify=True,
        verify_model="m",
    )
    base.update(over)
    return argparse.Namespace(**base)


# ── the secrets file ──────────────────────────────────────────────────────


def test_write_env_value_keeps_the_other_providers_keys(tmp_path):
    """proxy.env holds every provider's key; importing one must not drop them."""
    env = tmp_path / "proxy.env"
    env.write_text(
        "https_proxy=http://127.0.0.1:7898\n"
        "CMDC_API_KEY=user_abc\n"
        f"{cb.AUTH_ENV}=old-token\n"
        "CLINE_API_KEY=sk_xyz\n"
    )
    assert cb.write_env_value(env, "new-token") is True
    text = env.read_text()
    assert "CMDC_API_KEY=user_abc" in text
    assert "CLINE_API_KEY=sk_xyz" in text
    assert "https_proxy=http://127.0.0.1:7898" in text
    assert cb.read_env_value(env) == "new-token"
    # ...and exactly one line for our key, not an append alongside the old one
    assert sum(1 for ln in text.splitlines() if ln.startswith(cb.AUTH_ENV + "=")) == 1


def test_write_env_value_is_a_noop_when_unchanged(tmp_path):
    env = tmp_path / "proxy.env"
    cb.write_env_value(env, "same")
    before = env.stat().st_mtime_ns
    assert cb.write_env_value(env, "same") is False
    assert env.stat().st_mtime_ns == before


def test_unreadable_store_is_an_error_never_an_empty_rewrite(tmp_path):
    """magpie: "An unreadable providers.json is an error, never an empty list."

    Here the stakes are the other providers' keys: if the store exists but
    cannot be read, rewriting it from an empty read would wipe them all. A
    directory stands in for "exists but unreadable" so the test behaves the
    same for any uid.
    """
    store = tmp_path / "proxy.env"
    store.mkdir()
    with pytest.raises((cb.CredentialError, OSError)):
        cb.write_env_value(store, "new-token")


def test_write_is_atomic_and_leaves_no_debris(tmp_path):
    """A crash must never leave a half-written secrets file at full length."""
    env = tmp_path / "proxy.env"
    cb.write_env_value(env, "tok")
    cb.write_env_value(env, "tok2")
    assert cb.read_env_value(env) == "tok2"
    assert [p.name for p in tmp_path.iterdir()] == ["proxy.env"]


def test_secrets_file_is_0600(tmp_path):
    env = tmp_path / "proxy.env"
    cb.write_env_value(env, "tok")
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_missing_store_reads_as_none(tmp_path):
    assert cb.read_env_value(tmp_path / "nope.env") is None


# ── classifying an unusable login ─────────────────────────────────────────


def test_classify_reports_why_not_just_that_it_failed(tmp_path):
    missing = tmp_path / "absent.info"
    assert cb.classify_auth_file(missing)[0] == "missing"

    bad = tmp_path / "bad.info"
    bad.write_text("{not json")
    assert cb.classify_auth_file(bad)[0] == "badjson"

    noauth = tmp_path / "noauth.info"
    noauth.write_text(json.dumps({"account": {}}))
    assert cb.classify_auth_file(noauth)[0] == "no-auth"

    notoken = tmp_path / "notoken.info"
    notoken.write_text(json.dumps({"auth": {"refreshToken": "r"}}))
    assert cb.classify_auth_file(notoken)[0] == "no-token"

    sealed = tmp_path / "sealed.info"
    sealed.write_text(json.dumps({"auth": {"accessToken": {"$wbEncrypted": 1, "envelope": "x"}}}))
    status, detail = cb.classify_auth_file(sealed)
    assert status == "encrypted" and "desktop" in detail

    ok = _session_file(tmp_path / "ok.info")
    assert cb.classify_auth_file(ok)[0] == "ok"


def test_find_session_prefers_the_newest_usable_file(tmp_path, monkeypatch):
    auth = tmp_path / "auth"
    old = _session_file(auth / "old.info", token="old")
    new = _session_file(auth / "new.info", token="new")
    broken = auth / "broken.info"
    broken.write_text("{")
    now = time.time()
    for p, age in ((old, 500), (new, 10), (broken, 1)):
        os.utime(p, (now - age, now - age))
    monkeypatch.setattr(cb, "AUTH_GLOBS", [str(auth / "*.info")])

    sess, checked = cb.find_session(None)
    assert sess is not None and sess["token"] == "new"
    # the newest file is the broken one: reported with its reason, not skipped
    assert checked[0][0] == broken and checked[0][1] == "badjson"


def test_no_session_message_names_every_path_it_looked_at(tmp_path):
    broken = tmp_path / "b.info"
    broken.write_text("{")
    msg = cb.describe_no_session([(broken, "badjson", "not JSON")])
    assert "badjson" in msg and str(broken) in msg
    assert "codebuddy" in msg


def test_if_stale_is_silent_when_codebuddy_is_not_in_use(tmp_path, monkeypatch, capsys):
    """`ccp bridge restart` must not nag on a machine that never added CodeBuddy."""
    monkeypatch.setattr(cb, "AUTH_GLOBS", [str(tmp_path / "nothing" / "*.info")])
    env = tmp_path / "proxy.env"
    env.write_text("CMDC_API_KEY=user_abc\n")
    assert cb._import(_args(tmp_path / "none.info", env), quiet=True) == 0
    assert capsys.readouterr() == ("", "")
    assert env.read_text() == "CMDC_API_KEY=user_abc\n"


def test_if_stale_adopts_a_rotated_token(tmp_path, monkeypatch, capsys):
    """The CLI refreshed; proxy.env must catch up instead of going stale."""
    src = _session_file(tmp_path / "auth" / "x.info", token="rotated")
    monkeypatch.setattr(cb, "AUTH_GLOBS", [str(tmp_path / "auth" / "*.info")])
    env = tmp_path / "proxy.env"
    env.write_text(f"{cb.AUTH_ENV}=original\nCMDC_API_KEY=user_abc\n")

    assert cb._import(_args(src, env), quiet=True) == 0
    assert cb.read_env_value(env) == "rotated"
    assert "CMDC_API_KEY=user_abc" in env.read_text()
    assert "rotated" in capsys.readouterr().out

    # and a second run is silent: already in sync
    assert cb._import(_args(src, env), quiet=True) == 0
    assert capsys.readouterr().out == ""


# ── expiry, without ever refreshing ───────────────────────────────────────


def test_expired_login_is_reported_and_not_written(tmp_path, capsys):
    """ccp must not refresh: the CLI owns the rotating refresh token, and two
    refreshers of one token sign each other out. So an expired session is a
    message telling the user to run the CLI, not a repair attempt.
    """
    env = tmp_path / "proxy.env"
    env.write_text(f"{cb.AUTH_ENV}=stale\n")
    path = _session_file(tmp_path / "auth" / "x.info", token="dead", expires_in_days=-1)

    assert cb._import(_args(path, env), quiet=False) == 1
    err = capsys.readouterr().err
    assert "EXPIRED" in err and "codebuddy" in err
    # the dead token did not replace whatever was there
    assert cb.read_env_value(env) == "stale"


def test_expiry_unknown_is_tolerated():
    assert cb.expiry_info({}) == (None, "unknown")


def test_missing_refresh_token_is_called_out(tmp_path, capsys):
    """magpie refuses an access-token-only sign-in outright; for a session we do
    not own, naming the consequence is the honest equivalent."""
    env = tmp_path / "proxy.env"
    path = _session_file(tmp_path / "auth" / "x.info", refresh=None)

    assert cb._import(_args(path, env), quiet=False) == 0
    assert "cannot renew itself" in capsys.readouterr().out
    assert cb.read_env_value(env) is not None


# ── which gateway a login belongs to ──────────────────────────────────────
# CodeBuddy runs INTL and CN gateways that do not share tokens. Probing one at
# the other is a bare `401 invalid_token` — which the prune read as an expired
# login. The host is derived from the session's own `auth.domain`.


def test_api_base_follows_the_logins_domain():
    assert cb.api_base({"domain": "www.codebuddy.cn"}) == cb.CN_API_BASE
    assert cb.api_base({"domain": "copilot.tencent.com"}) == cb.CN_API_BASE
    assert cb.api_base({"domain": "tencent.sso.codebuddy.cn"}) == cb.CN_API_BASE
    assert cb.api_base({"domain": "www.codebuddy.ai"}) == cb.INTL_API_BASE
    # Unknown or absent: default to INTL rather than guessing CN.
    assert cb.api_base({}) == cb.INTL_API_BASE
    assert cb.api_base(None) == cb.INTL_API_BASE


def test_api_base_env_override_wins(monkeypatch):
    monkeypatch.setenv(cb.API_BASE_ENV, "https://example.test/v2/")
    assert cb.api_base({"domain": "www.codebuddy.cn"}) == "https://example.test/v2"


def test_chat_url_is_the_bases_chat_route():
    assert cb.chat_url({"domain": "www.codebuddy.cn"}) == (
        "https://copilot.tencent.com/v2/chat/completions"
    )
    assert cb.chat_url({"domain": "www.codebuddy.ai"}) == cb.GATEWAY


def test_session_domain_reaches_the_host(tmp_path):
    """The end-to-end path: a CN login file selects the CN gateway, not the
    INTL default the module used to hard-code."""
    path = _session_file(tmp_path / "cn.info")
    doc = json.loads(path.read_text())
    doc["auth"]["domain"] = "www.codebuddy.cn"
    path.write_text(json.dumps(doc))

    sess, _ = cb.find_session(str(path))
    assert sess is not None and sess["domain"] == "www.codebuddy.cn"
    assert cb.api_base(sess) == cb.CN_API_BASE


def test_verify_sends_the_conversation_headers_the_channel_check_reads(monkeypatch):
    """Without the three headers the gateway answers 11128 and the probe lies.

    The check is the whole reason this provider exists
    (docs/workbuddy-channel-check.md), so the probe must send them.
    """
    seen = {}

    class _Resp:
        status = 200

        def read(self, _n):
            return b"data: {\"choices\": []}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None, context=None):
        seen.update({k.lower(): v for k, v in req.header_items()})
        seen["_url"] = req.full_url
        seen["_body"] = req.data.decode() if req.data else ""
        return _Resp()

    monkeypatch.setattr(cb.urllib.request, "urlopen", fake_urlopen)
    ok, detail = cb.verify("tok", model="deepseek-v4.1-flash")
    assert ok, detail
    for h in ("X-Conversation-ID", "X-Conversation-Message-ID", "X-Conversation-Request-ID"):
        assert seen.get(h.lower()), f"the probe did not send {h}"
    # and the body must open with a system message ("first message is not
    # system prompt" is the other half of the check)
    body = json.loads(seen["_body"])
    assert body["messages"][0]["role"] == "system"
    assert body["stream"] is True, "the gateway refuses a non-stream request"
