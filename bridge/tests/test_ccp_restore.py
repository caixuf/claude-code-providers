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
