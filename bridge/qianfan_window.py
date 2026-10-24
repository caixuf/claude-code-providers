#!/usr/bin/env python3
"""Swap Qianfan into (or out of) the routing groups, then re-deploy the bridge.

Qianfan's Token Plan is metered by wall-clock time: full price by day, 20% of
that after 21:00. So the honest default is "not a member", and the plan earns
its place only during the hours it is cheap. That cannot live in LiteLLM —
there is no time-of-day routing — so it is a rewrite of the group block plus a
re-render, driven by systemd timers at 21:00 and 08:00.

The block is wrapped in markers so this never has to parse YAML and never
rewrites anything it did not put there:

    # >>> qianfan-window
    ...
    # <<< qianfan-window

Only `main` carries the markers. `pro` and `flash` are the same model under
other names and always follow `main`, so there is one place for this state to
be wrong instead of three.

    qianfan_window.py --on     # 21:00 — Qianfan first, OpenDesign stands by
    qianfan_window.py --off    # 08:00 — OpenDesign first, Cline stands by
    qianfan_window.py --status # what the file says right now, no writes

`--off` is the default when no argument is given, because the failure that
costs money is forgetting to switch back, not forgetting to switch in.

Nothing here restarts anything by itself. It exits 0 when the file already
holds the requested state, so the timers are free to run on every boot.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROFILES = REPO / "registry" / "profiles.yaml"
CCP = REPO / "bin" / "ccp"

BEGIN = "    # >>> qianfan-window"
END = "    # <<< qianfan-window"

# Qianfan is fastest after dark; OpenDesign is the daytime default; Cline is
# the stand-in both ways because it has no time-dependent price.
ON_BLOCK = """{begin}
    members:
    - qianfan-deepseek-v4.1-flash
    fallback:
    - opendesign-deepseek-v4.1-flash
    - cline-deepseek-v4.1-flash
{end}"""

OFF_BLOCK = """{begin}
    members:
    - opendesign-deepseek-v4.1-flash
    fallback:
    - cline-deepseek-v4.1-flash
{end}"""


def current_state(text: str) -> str:
    """'on', 'off', or 'malformed' — read from the block between the markers.

    Matched on a list item, not on the substring `qianfan-`: the surrounding
    prose explains why Qianfan is or is not here, so a substring test reads
    the comment as the setting and reports `on` for the block that says
    `opendesign`.
    """
    try:
        a = text.index(BEGIN)
        b = text.index(END, a)
    except ValueError:
        return "malformed"
    for line in text[a:b].splitlines():
        item = line.strip()
        if item.startswith("- ") and "qianfan-" in item:
            return "on"
    return "off"


def rewrite(text: str, state: str) -> str:
    a = text.index(BEGIN)
    b = text.index(END, a) + len(END)
    block = (ON_BLOCK if state == "on" else OFF_BLOCK).format(begin=BEGIN, end=END)
    return text[:a] + block + text[b:]


def apply(state: str, *, dry_run: bool = False) -> int:
    text = PROFILES.read_text(encoding="utf-8")
    now = current_state(text)
    if now == "malformed":
        print(
            f"error: the {BEGIN.strip()} / {END.strip()} markers are missing from {PROFILES}.\n"
            "       Nothing was written — re-add them around the main group's members.",
            file=sys.stderr,
        )
        return 2
    if now == state:
        print(f"qianfan window already {state}; nothing to do")
        return 0
    if dry_run:
        print(f"would switch qianfan {now} -> {state}")
        return 0

    PROFILES.write_text(rewrite(text, state), encoding="utf-8")
    print(f"qianfan window {now} -> {state}")

    # render reads the registry; deploy writes LiteLLM's config; restart makes
    # LiteLLM read it. All three, or the file on disk and the running proxy
    # disagree — which is exactly how a "switched" plan keeps billing at the
    # old rate.
    for args in (["render"], ["bridge", "deploy"], ["bridge", "restart"]):
        r = subprocess.run([str(CCP), *args], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"error: ccp {' '.join(args)} failed:\n{r.stdout}{r.stderr}", file=sys.stderr)
            return r.returncode or 1
    print("bridge re-deployed and restarted")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Swap Qianfan in or out of the routing groups.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--on", action="store_true", help="Qianfan first (the cheap window)")
    g.add_argument("--off", action="store_true", help="OpenDesign first (default)")
    g.add_argument("--status", action="store_true", help="print the current state")
    p.add_argument("--dry-run", action="store_true", help="say what would change, write nothing")
    args = p.parse_args(argv)

    state = current_state(PROFILES.read_text(encoding="utf-8"))
    if args.status:
        print(f"qianfan window: {state}")
        return 0 if state != "malformed" else 2

    want = "on" if args.on else "off"
    return apply(want, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
