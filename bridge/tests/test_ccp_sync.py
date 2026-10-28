"""`ccp sync` repairs the live profile, not only the installed ones.

The active profile is a *copy* of a provider profile, so it drifts exactly the
way a profile drifts - and the drift that hurts is the credential: when the
bridge's key was renamed, the copy kept the old one and every request failed
with a 400 that named neither the key nor the bridge.
"""
from __future__ import annotations

import json
import os
import pathlib
import shlex
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
CCP = REPO / "bin" / "ccp"

BRIDGED = {
    "model": "yjapi-deepseek-v4.1-flash[1m]",
    "env": {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
        "ANTHROPIC_AUTH_TOKEN": "sk-cmdc-litellm-local",
        "ANTHROPIC_MODEL": "yjapi-deepseek-v4.1-flash[1m]",
    },
}

NATIVE = {
    "model": "MiniMax-M3.1-Flash",
    "env": {
        "ANTHROPIC_BASE_URL": "https://api.minimax.cn/anthropic",
        "ANTHROPIC_AUTH_TOKEN": "sk-the-vendors-own-key",
        "ANTHROPIC_MODEL": "MiniMax-M3.1-Flash",
    },
}


@pytest.fixture
def sandbox(tmp_path):
    home = tmp_path / "home"
    claude = home / ".claude"
    claude.mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "systemctl"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)

    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "CLAUDE_DIR": str(claude),
        "CCP_REPO_DIR": str(REPO),
        "PATH": f"{bindir}:{env['PATH']}",
    })
    return env, claude


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


def test_the_live_profile_gets_the_bridge_credential(sandbox):
    env, claude = sandbox
    settings = claude / "settings.json"
    write_json(settings, BRIDGED)

    out = source(env, "reconcile_active_settings")
    assert out.returncode == 0, out.stderr

    data = read_json(settings)
    assert data["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-ccp-litellm-local"
    # the rest of the profile is the user's, and stays
    assert data["env"]["ANTHROPIC_MODEL"] == "yjapi-deepseek-v4.1-flash[1m]"
    assert data["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4000"
    # and the old file is kept
    assert len(list(claude.glob("settings.json.bak.*"))) == 1


def test_a_native_profile_keeps_the_vendors_credential(sandbox):
    env, claude = sandbox
    settings = claude / "settings.json"
    write_json(settings, NATIVE)

    out = source(env, "reconcile_active_settings || true")
    assert out.returncode == 0, out.stderr

    data = read_json(settings)
    assert data["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-the-vendors-own-key"
    assert not list(claude.glob("settings.json.bak.*"))


def test_a_profile_that_is_already_right_is_left_alone(sandbox):
    env, claude = sandbox
    settings = claude / "settings.json"
    write_json(settings, {
        "env": {
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
            "ANTHROPIC_AUTH_TOKEN": "sk-ccp-litellm-local",
        },
    })

    out = source(env, "reconcile_active_settings || true")
    assert out.returncode == 0, out.stderr
    assert not list(claude.glob("settings.json.bak.*"))
