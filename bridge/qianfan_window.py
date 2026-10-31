#!/usr/bin/env python3
"""Swap Qianfan into (or out of) the routing groups, then re-deploy the bridge.

Qianfan's Token Plan is metered by wall-clock time: full price by day, 20% of
that after 21:00. So the honest default is "not a member", and the plan earns
its place only during the hours it is cheap. That cannot live in LiteLLM --
there is no time-of-day routing -- so it is a rewrite plus a re-render, driven
by systemd timers at 21:00 and 08:00.

Why the state is NOT in registry/profiles.yaml
----------------------------------------------
The first version wrote into the committed file, and within a day that shipped
the wrong default twice: a commit taken at 23:00 recorded "Qianfan first" as
what a fresh clone should start with, and a git status that is never clean
hides real edits. The committed file now always describes the daytime
arrangement; this script writes registry/profiles.local.yaml, which is
git-ignored and which render merges over the top-level keys it names.

    qianfan_window.py --on         # 21:00 - Qianfan first for main
    qianfan_window.py --off        # 08:00 - OpenDesign first for main
    qianfan_window.py --status     # what is in effect right now, no writes
    qianfan_window.py --show       # the override file as it stands

`pro` is NOT part of the schedule. It is the slot a human points at a chosen
premium model, so the timer leaves it to registry/profiles.yaml.

--off is the default when no argument is given, because the failure that costs
money is forgetting to switch back, not forgetting to switch in.

Nothing here restarts anything unless a change was actually written, so the
timers are free to run on every boot.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OVERLAY = REPO / "registry" / "profiles.local.yaml"
CCP = REPO / "bin" / "ccp"

ON = """# Written by bridge/qianfan_window.py -- inside the 21:00-08:00 window, when
# Qianfan's Token Plan costs 20% of its daytime price. Git-ignored on purpose:
# this is wall-clock state, not a curation choice, and committing it is how
# "it was 21:00 when I committed" became a default.
#
# `main` ONLY, and nothing else. Every group named here is replaced outright for
# the duration of the window, so naming a group the schedule does not own would
# let the timer silently revert a change someone made to it -- which is exactly
# what happened to `pro`. An unnamed group keeps whatever registry/profiles.yaml
# says, so `pro` (a human's pick) and `flash` (fixed) need no entry here.
groups:
  main:
    members:
    - qianfan-deepseek-v4.1-flash-1017
    fallback:
    - opendesign-deepseek-v4.1-flash
    - cline-deepseek-v4.1-flash
"""


def current_state() -> str:
    """'on' or 'off'.

    No file at all is the off state: render then uses the committed values,
    which already are the daytime arrangement. Writing an explicit off file
    would be a second place for the same truth to disagree with itself.
    """
    if not OVERLAY.is_file():
        return "off"
    for line in OVERLAY.read_text(encoding="utf-8").splitlines():
        item = line.strip()
        if item.startswith("- ") and "qianfan-" in item:
            return "on"
    return "off"


def target_content(state: str) -> str | None:
    """What the overlay file must contain in `state`; None means "no file"."""
    return ON if state == "on" else None


def overlay_is_stale(want: str | None) -> bool:
    """True when the file on disk is not exactly `want`.

    Comparing content, not a semantic guess, is what makes an upgrade
    self-healing: `current_state()` only asked "does some qianfan member appear
    here", so a file written by an older version still answered "already on" and
    was left alone -- which is how a stale overlay kept overwriting `pro`.
    """
    if want is None:
        return OVERLAY.is_file()
    return not OVERLAY.is_file() or OVERLAY.read_text(encoding="utf-8") != want


def apply(state: str, *, dry_run: bool = False) -> int:
    now, want = current_state(), target_content(state)
    if not overlay_is_stale(want):
        print("qianfan window already %s; overlay matches; nothing to do" % state)
        return 0
    if dry_run:
        print("would write the %s overlay (currently %s) and re-deploy" % (state, now))
        return 0

    if want is None:
        OVERLAY.unlink(missing_ok=True)
    else:
        OVERLAY.parent.mkdir(parents=True, exist_ok=True)
        OVERLAY.write_text(want, encoding="utf-8")
    print("qianfan window %s -> %s" % (now, state))

    for step in (["render"], ["bridge", "deploy"], ["bridge", "restart"]):
        r = subprocess.run([str(CCP), *step], capture_output=True, text=True)
        if r.returncode != 0:
            print("error: ccp %s failed:\n%s%s" % (" ".join(step), r.stdout, r.stderr), file=sys.stderr)
            # The file already is the new one, so a plain retry would see it as
            # current and skip the deploy. Dropping it makes the next run take
            # the slow path instead of leaving config written but unserved.
            if want is not None:
                OVERLAY.unlink(missing_ok=True)
            return r.returncode or 1
    print("bridge re-deployed and restarted")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Swap Qianfan in or out of the routing groups.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--on", action="store_true", help="Qianfan first (the cheap window)")
    g.add_argument("--off", action="store_true", help="OpenDesign first (default)")
    g.add_argument("--status", action="store_true", help="print the current state")
    g.add_argument("--show", action="store_true", help="print the override file")
    p.add_argument("--dry-run", action="store_true", help="say what would change, write nothing")
    args = p.parse_args(argv)

    if args.status:
        print("qianfan window: %s" % current_state())
        return 0
    if args.show:
        if OVERLAY.is_file():
            print(OVERLAY.read_text(encoding="utf-8"))
        else:
            print("(no override: committed daytime defaults are in effect)")
        return 0

    want = "on" if args.on else "off"
    return apply(want, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
