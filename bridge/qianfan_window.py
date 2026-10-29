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

    qianfan_window.py --on         # 21:00 - Qianfan first for main/pro
    qianfan_window.py --off        # 08:00 - OpenDesign first for main/pro
    qianfan_window.py --status     # what is in effect right now, no writes
    qianfan_window.py --show       # the override file as it stands

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
groups:
  main:
    members:
    - qianfan-deepseek-v4.1-flash-1017
    fallback:
    - opendesign-deepseek-v4.1-flash
    - cline-deepseek-v4.1-flash
  pro:
    members:
    - qianfan-deepseek-v4.1-flash-1017
    fallback:
    - opendesign-deepseek-v4.1-flash
    - cline-deepseek-v4.1-flash
  flash:
    members:
    - beam-free
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


def apply(state: str, *, dry_run: bool = False) -> int:
    now = current_state()
    if now == state:
        print("qianfan window already %s; nothing to do" % state)
        return 0
    if dry_run:
        print("would switch qianfan %s -> %s" % (now, state))
        return 0

    if state == "on":
        OVERLAY.parent.mkdir(parents=True, exist_ok=True)
        OVERLAY.write_text(ON, encoding="utf-8")
    elif OVERLAY.is_file():
        OVERLAY.unlink()
    print("qianfan window %s -> %s" % (now, state))

    for step in (["render"], ["bridge", "deploy"], ["bridge", "restart"]):
        r = subprocess.run([str(CCP), *step], capture_output=True, text=True)
        if r.returncode != 0:
            print("error: ccp %s failed:\n%s%s" % (" ".join(step), r.stdout, r.stderr), file=sys.stderr)
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
