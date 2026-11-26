"""The Trae adapter must be installed and enabled, not merely started.

`ccp restore` intentionally disables ccp's services. A later `ccp bridge start`
or `ccp trae` must make the route survive the next reboot; otherwise the user
finds a stopped :4100 only when a session needs it.
"""

from __future__ import annotations

import os
import pathlib
import shlex
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[2]
CCP = REPO / "bin" / "ccp"


def _sandbox(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "systemctl.log"
    stub = bindir / "systemctl"
    stub.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> " + shlex.quote(str(log)) + "\nexit 0\n"
    )
    stub.chmod(0o755)
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "CCP_REPO_DIR": str(REPO),
        "PATH": f"{bindir}:{env['PATH']}",
    })
    return env, home, log


def _source(env, body):
    script = f"export CCP_LIB_ONLY=1; source {shlex.quote(str(CCP))}; {body}"
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)


def test_trae_unit_is_rendered_for_the_actual_checkout(tmp_path):
    env, home, log = _sandbox(tmp_path)
    out = _source(env, "ensure_trae_unit_installed")
    assert out.returncode == 0, out.stderr

    unit = home / ".config/systemd/user/ccp-trae-bridge.service"
    text = unit.read_text()
    assert f"WorkingDirectory={REPO / 'bridge'}" in text
    assert f"ExecStart=" in text and str(REPO / "bridge" / "trae_bridge.py") in text
    assert "%h/code/claude-code-providers" not in text
    assert "--user daemon-reload" in log.read_text()


def test_start_trae_enables_the_unit_for_boot(tmp_path):
    env, home, log = _sandbox(tmp_path)
    out = _source(env, "manage_bridge start-trae")
    assert out.returncode == 0, out.stderr
    assert "--user enable --now ccp-trae-bridge.service" in log.read_text()
    assert (home / ".config/systemd/user/ccp-trae-bridge.service").is_file()
