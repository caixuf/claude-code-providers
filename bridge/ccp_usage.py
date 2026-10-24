#!/usr/bin/env python3
"""`ccp usage` — what has actually been spent, per day, model or provider.

Only the bridge sees Anthropic-shaped `usage` for every provider, so the ledger
written by sse_watchdog is the one place a cross-provider total can come from.
Providers behind a native endpoint (deepseek, minimax, opendesign, codex) do
not traverse LiteLLM at all, so LiteLLM's own spend table can never answer this.

No prices are baked in here. Every plan on this machine prices cache reads,
cache writes and fresh input differently, and a wrong multiplier is worse than
no number — the counts are shown, and the caller multiplies.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from usage_ledger import FIELDS, aggregate, read_ledger, sort_keys  # noqa: E402

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"


def _n(v: int) -> str:
    """Token counts are read at a glance, so 12.3k beats 12345."""
    if v >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if v >= 10_000:
        return f"{v / 1000:.1f}k"
    return str(v)


def _default_path() -> Path:
    return Path.home() / ".local/state/ccp/usage.jsonl"


def _since(records: list[dict], args) -> list[dict]:
    if args.all:
        return records
    days = args.days
    if days is None:
        days = 1 if args.today else 7
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    return [r for r in records if (r.get("ts") or "")[:10] >= cutoff]


def _table(headers: list[str], rows: list[list[str]], aligns: str = "") -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    out = []
    head = "  ".join(
        (h.rjust(widths[i]) if aligns[i : i + 1] == "r" else h.ljust(widths[i]))
        for i, h in enumerate(headers)
    )
    out.append(BOLD + head + RESET)
    for row in rows:
        out.append(
            "  ".join(
                (c.rjust(widths[i]) if aligns[i : i + 1] == "r" else c.ljust(widths[i]))
                for i, c in enumerate(row)
            )
        )
    return "\n".join(out)


def cmd_summary(records, args) -> int:
    if not records:
        print(f"{YELLOW}No usage recorded yet.{RESET}")
        print(f"{DIM}The bridge writes it to {args.path}{RESET}")
        print(f"{DIM}Requests reach the ledger only while the watchdog is running:{RESET}")
        print("  ccp bridge status")
        return 0

    total_input = sum(r.get("input_tokens", 0) or 0 for r in records)
    total_output = sum(r.get("output_tokens", 0) or 0 for r in records)
    cache_read = sum(r.get("cache_read_input_tokens", 0) or 0 for r in records)
    cache_write = sum(r.get("cache_creation_input_tokens", 0) or 0 for r in records)
    billed = total_input + cache_read + cache_write
    grand = billed + total_output

    print(f"{BOLD}Usage{RESET} {DIM}({len(records)} requests){RESET}")
    print()
    print(f"  fresh input   {_n(total_input):>8}")
    print(f"  cache read    {_n(cache_read):>8}  {DIM}(cheap tier){RESET}")
    print(f"  cache write   {_n(cache_write):>8}")
    print(f"  {BOLD}input total   {_n(billed):>8}{RESET}")
    print(f"  output        {_n(total_output):>8}")
    print(f"  {BOLD}{GREEN}grand total   {_n(grand):>8}{RESET}")

    if cache_read:
        share = 100.0 * cache_read / billed
        print()
        print(f"  {DIM}{share:.1f}% of input was served from cache{RESET}")

    by_day = aggregate(records, "day")
    keys = sort_keys(by_day, "day")
    rows = []
    for k in keys:
        b = by_day[k]
        rows.append(
            [
                k,
                str(b.requests),
                _n(b.billed_input),
                _n(b.output_tokens),
                _n(b.total),
            ]
        )
    print()
    print(_table(["day", "req", "input", "output", "total"], rows, "rrrr"))
    return 0


def cmd_group(records, args, key: str) -> int:
    if not records:
        print(f"{YELLOW}No usage recorded yet.{RESET}")
        return 0
    grouped = aggregate(records, key)
    keys = sort_keys(grouped, key)
    if args.limit:
        keys = keys[: args.limit]
    rows = []
    for k in keys:
        b = grouped[k]
        rows.append(
            [
                k,
                str(b.requests),
                _n(b.billed_input),
                _n(b.output_tokens),
                _n(b.total),
            ]
        )
    label = {"model": "model", "provider": "provider"}[key]
    print(_table([label, "req", "input", "output", "total"], rows, "lrrrr"))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="ccp usage",
        description="Token usage recorded by the bridge.",
        epilog=(
            "Prices are not applied: every plan here prices fresh input, cache "
            "reads and cache writes differently, so the counts are shown and "
            "you multiply."
        ),
    )
    p.add_argument("--path", default=str(_default_path()), help="ledger file")
    p.add_argument("--today", action="store_true", help="today only (UTC days)")
    p.add_argument("--days", type=int, default=None, help="last N days (default 7)")
    p.add_argument("--all", action="store_true", help="everything in the ledger")
    p.add_argument("--by-model", action="store_true", help="group by model")
    p.add_argument("--by-provider", action="store_true", help="group by provider")
    p.add_argument("--limit", type=int, default=0, help="show at most N groups")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    args = p.parse_args(argv)

    records = _since(read_ledger(args.path), args)

    if args.json:
        import json

        out = {
            "requests": len(records),
            "fast": {f: sum(r.get(f, 0) or 0 for r in records) for f in FIELDS},
        }
        for key in ("day", "model", "provider"):
            grouped = aggregate(records, key)
            out[key] = {
                k: {
                    "requests": b.requests,
                    **{f: getattr(b, f) for f in FIELDS},
                    "total": b.total,
                }
                for k in sort_keys(grouped, key)
            }
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    if args.by_model:
        return cmd_group(records, args, "model")
    if args.by_provider:
        return cmd_group(records, args, "provider")
    return cmd_summary(records, args)


if __name__ == "__main__":
    raise SystemExit(main())
