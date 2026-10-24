"""Tests for `ccp restore` — returning Claude Code to its pre-ccp settings.

The failure this guards against is silent: ccp replaces settings.json
wholesale, so "restore" must either find a file that really predates ccp or
refuse. Reusing a ccp-rendered backup would leave the user pointed at a bridge
that the same command just stopped.
"""

import json
import os
import pathlib
import shlex
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
CCP = REPO / "bin" / "ccp"

MANAGED = {
    "model": "qwen3.8-max",
    "permissions": {"allow": ["Bash(git status)"]},
    "env": {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:4100",
        "ANTHROPIC_AUTH_TOKEN": "local-bridge-token",
        "ANTHROPIC_MODEL": "qwen3.8-max",
        "ANTHROPIC_DEFAULT_FABLE_MODEL": "Doubao-Seed-2.1-pro",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000",
        "SOME_USER_SETTING": "keep-me",
    },
}

PRISTINE = {
    "model": "opus",
    "permissions": {"allow": ["Bash(git status)"]},
    "env": {"SOME_USER_SETTING": "keep-me"},
}

UNITS = [
    "litellm-cmdc.service",
    "ccp-sse-watchdog.service",
    "ccp-trae-bridge.service",
]


@pytest.fixture
def sandbox(tmp_path):
    home = tmp_path / "home"
    claude = home / ".claude"
    claude.mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "systemctl.log"
    stub = bindir / "systemctl"
    stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {shlex.quote(str(log))}\nexit 0\n')
    stub.chmod(0o755)

    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "CLAUDE_DIR": str(claude),
        "PATH": f"{bindir}:{env['PATH']}",
    })
    return env, claude, log


def write_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def run_ccp(env, *args):
    return subprocess.run(
        ["bash", str(CCP), *args], env=env, capture_output=True, text=True
    )


def source(env, body):
    script = f"export CCP_LIB_ONLY=1; source {shlex.quote(str(CCP))}; {body}"
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_restore_prefers_snapshot_and_stops_services(sandbox):
    env, claude, log = sandbox
    settings = claude / "settings.json"
    write_json(settings, MANAGED)
    write_json(claude / "settings.json.ccp-original", PRISTINE)

    out = run_ccp(env, "restore", "--yes")

    assert out.returncode == 0, out.stderr
    assert read_json(settings) == PRISTINE
    calls = log.read_text().splitlines()
    for unit in UNITS:
        assert f"--user stop {unit}" in calls
        assert f"--user disable {unit}" in calls
    assert len(list(claude.glob("settings.json.bak.*"))) == 1


def test_dry_run_changes_nothing(sandbox):
    env, claude, log = sandbox
    settings = claude / "settings.json"
    write_json(settings, MANAGED)
    write_json(claude / "settings.json.ccp-original", PRISTINE)

    out = run_ccp(env, "restore", "--dry-run")

    assert out.returncode == 0, out.stderr
    assert read_json(settings) == MANAGED
    assert not log.exists()
    assert not list(claude.glob("settings.json.bak.*"))


def test_falls_back_to_oldest_backup_that_ccp_did_not_render(sandbox):
    env, claude, _ = sandbox
    settings = claude / "settings.json"
    write_json(settings, MANAGED)

    older = claude / "settings.json.bak.20260101-000000"
    newer = claude / "settings.json.bak.20260102-000000"
    write_json(older, PRISTINE)
    write_json(newer, MANAGED)
    os.utime(older, (1_000, 1_000))
    os.utime(newer, (2_000, 2_000))

    out = run_ccp(env, "restore", "--yes")

    assert out.returncode == 0, out.stderr
    assert read_json(settings) == PRISTINE


def test_refuses_when_only_ccp_rendered_settings_exist(sandbox):
    env, claude, log = sandbox
    settings = claude / "settings.json"
    write_json(settings, MANAGED)

    out = run_ccp(env, "restore", "--yes")

    assert out.returncode == 1
    assert "No pre-ccp settings found" in out.stderr
    assert read_json(settings) == MANAGED
    assert not log.exists()


def test_strip_removes_only_ccp_keys(sandbox):
    env, claude, _ = sandbox
    settings = claude / "settings.json"
    write_json(settings, MANAGED)

    out = run_ccp(env, "restore", "--yes", "--strip")

    assert out.returncode == 0, out.stderr
    restored = read_json(settings)
    assert "model" not in restored
    assert restored["permissions"] == MANAGED["permissions"]
    assert restored["env"] == {"SOME_USER_SETTING": "keep-me"}


def test_from_restores_an_explicit_file(sandbox, tmp_path):
    env, claude, _ = sandbox
    settings = claude / "settings.json"
    write_json(settings, MANAGED)
    chosen = tmp_path / "chosen.json"
    write_json(chosen, PRISTINE)

    out = run_ccp(env, "restore", "--yes", "--from", str(chosen))

    assert out.returncode == 0, out.stderr
    assert read_json(settings) == PRISTINE


def test_capture_skips_a_ccp_rendered_file(sandbox):
    env, claude, _ = sandbox
    write_json(claude / "settings.json", MANAGED)

    out = source(env, "capture_pristine_settings")

    assert out.returncode == 0, out.stderr
    assert not (claude / "settings.json.ccp-original").exists()


def test_capture_saves_a_pristine_file_once(sandbox):
    env, claude, _ = sandbox
    settings = claude / "settings.json"
    write_json(settings, PRISTINE)

    assert source(env, "capture_pristine_settings").returncode == 0
    snapshot = claude / "settings.json.ccp-original"
    assert read_json(snapshot) == PRISTINE
    assert oct(snapshot.stat().st_mode & 0o777) == "0o600"

    write_json(settings, {"model": "sonnet"})
    assert source(env, "capture_pristine_settings").returncode == 0
    assert read_json(snapshot) == PRISTINE  # never overwritten


# ── the fingerprint: which files count as ccp's own ────────────────────
#
# The predicate decides two things at once: whether `capture_pristine_settings`
# will save a file as the pre-ccp original, and whether `restore` may hand a
# backup back as "original". Getting it too wide silently makes both useless —
# every file is "ccp's", so nothing is ever captured and restore refuses while
# holding a directory full of backups. That is exactly what happened when
# CLAUDE_CODE_AUTO_COMPACT_WINDOW was treated as a ccp marker: it was in the
# user's hand-written settings long before ccp, so all 113 real backups were
# classified as ccp-rendered and `ccp restore` answered "No pre-ccp settings
# found".

# A settings.json the user configured themselves before ccp existed. It has
# the compact-window key, because people set that by hand; it has no Fable
# tier, because ccp invented that one.
USER_OWN_WITH_COMPACT_WINDOW = {
    "model": "MiniMax-M3[1m]",
    "env": {
        "ANTHROPIC_BASE_URL": "https://api.minimax.cn/anthropic",
        "ANTHROPIC_AUTH_TOKEN": "sk-api-user-own",
        "ANTHROPIC_MODEL": "MiniMax-M3[1m]",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000",
    },
}

# An early ccp profile: it points at a ccp bridge but predates the Fable tier.
EARLY_CCP_PROFILE = {
    "model": "deepseek/deepseek-v4.1-flash",
    "env": {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
        "ANTHROPIC_AUTH_TOKEN": "sk-ccp-local",
        "ANTHROPIC_MODEL": "deepseek/deepseek-v4.1-flash",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1048576",
    },
}

# The user's own gateway on a public URL: still theirs, whatever it serves.
USER_OWN_PUBLIC_GATEWAY = {
    "model": "step-5-preview",
    "env": {
        "ANTHROPIC_BASE_URL": "https://api.stepfun.com/step_plan",
        "ANTHROPIC_MODEL": "step-5-preview",
    },
}


def is_managed(env, path):
    out = source(env, f"is_ccp_managed_settings {shlex.quote(str(path))} && echo YES || echo NO")
    return out.stdout.strip().endswith("YES")


def test_compact_window_alone_does_not_make_a_file_ccp(sandbox):
    """The bug: a user-owned file with the compact-window key was 'ccp's'."""
    env, claude, _ = sandbox
    p = claude / "settings.json"
    write_json(p, USER_OWN_WITH_COMPACT_WINDOW)
    assert not is_managed(env, p), (
        "a hand-written compact-window setting must not be mistaken for a "
        "ccp-rendered profile"
    )


def test_fable_tier_marks_a_file_as_ccp(sandbox):
    env, claude, _ = sandbox
    p = claude / "settings.json"
    write_json(p, MANAGED)
    assert is_managed(env, p)


def test_early_profile_pointing_at_a_ccp_bridge_is_ccp(sandbox):
    """Early ccp had no Fable tier; the loopback bridge still identifies it."""
    env, claude, _ = sandbox
    p = claude / "settings.json"
    write_json(p, EARLY_CCP_PROFILE)
    assert is_managed(env, p)


def test_a_public_gateway_is_the_users_own(sandbox):
    env, claude, _ = sandbox
    p = claude / "settings.json"
    write_json(p, USER_OWN_PUBLIC_GATEWAY)
    assert not is_managed(env, p)


def test_capture_keeps_a_user_file_that_has_the_compact_window(sandbox):
    """The consequence of the bug: the snapshot was never taken."""
    env, claude, _ = sandbox
    settings = claude / "settings.json"
    write_json(settings, USER_OWN_WITH_COMPACT_WINDOW)

    assert source(env, "capture_pristine_settings").returncode == 0

    snapshot = claude / "settings.json.ccp-original"
    assert snapshot.exists(), "the pre-ccp original was silently not captured"
    assert read_json(snapshot) == USER_OWN_WITH_COMPACT_WINDOW


def test_restore_finds_an_original_among_ccp_backups(sandbox):
    """End to end: originals exist, ccp files surround them, restore must find one."""
    env, claude, _ = sandbox
    settings = claude / "settings.json"
    write_json(settings, MANAGED)

    original = claude / "settings.json.bak.20260923-000000"
    rendered = claude / "settings.json.bak.20260929-000000"
    write_json(original, USER_OWN_WITH_COMPACT_WINDOW)
    write_json(rendered, MANAGED)

    out = run_ccp(env, "restore", "--dry-run")

    assert out.returncode == 0, out.stderr
    assert str(original) in out.stdout
    assert "no API key or auth token" not in out.stdout


def test_restore_warns_when_the_original_carries_no_credential(sandbox):
    """A credential-less original is only valid with OAuth — say so, don't hide it."""
    env, claude, _ = sandbox
    settings = claude / "settings.json"
    write_json(settings, MANAGED)

    no_creds = {k: v for k, v in USER_OWN_WITH_COMPACT_WINDOW.items()}
    no_creds["env"] = {k: v for k, v in no_creds["env"].items()
                       if k not in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")}
    original = claude / "settings.json.bak.20260923-000000"
    write_json(original, no_creds)

    out = run_ccp(env, "restore", "--dry-run")

    assert out.returncode == 0, out.stderr
    assert "no API key or auth token" in out.stdout
