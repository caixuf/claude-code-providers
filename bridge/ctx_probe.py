#!/usr/bin/env python3
"""Measure a model's real context window with a needle-in-a-haystack probe.

A catalogue can publish ``prompt_max_tokens`` (what its own CLI is willing to
send) instead of the model's window, and a request that returns 200 with empty
content is not proof it read the prompt. This probe places a short code at the
very end of an otherwise uninteresting body and asks the model to repeat it.
Only an answer that contains the code proves the prompt was read to the end.

Usage:
  python3 ctx_probe.py --model glm-5.3-flashx
  python3 ctx_probe.py --model glm-5.3-flashx --sizes 200000,400000,900000
  python3 ctx_probe.py --model glm-5.3-flashx --mode output --target 20000
  python3 ctx_probe.py --endpoint http://127.0.0.1:4100 --model <id> --json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = "http://127.0.0.1:4100"
DEFAULT_SIZES = (184000, 200000, 400000, 900000, 1050000)
CODE = "ZEBRA-7319"
FILLER = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu "


def build_prompt(tokens: int) -> str:
    """A prompt of roughly ``tokens`` tokens with the code at the very end."""
    target_chars = max(0, tokens * 4 - len(CODE) - 200)
    body = (FILLER * (target_chars // len(FILLER) + 1))[:target_chars]
    return (
        "Read the whole text. Reply with only the code on the final line.\n\n"
        f"{body}\n\nCODE: {CODE}\nOnly the code:"
    )


def estimate_tokens(text: str) -> int:
    return max(1, len(text.encode("utf-8")) // 4)


def ask_doc(endpoint: str, model: str, prompt: str, *, timeout: float,
            max_tokens: int) -> dict:
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ask(endpoint: str, model: str, prompt: str, *, timeout: float, max_tokens: int) -> str:
    doc = ask_doc(endpoint, model, prompt, timeout=timeout, max_tokens=max_tokens)
    blocks = doc.get("content") or []
    return "".join(b.get("text", "") for b in blocks if isinstance(b, dict))


def build_output_prompt(target: int) -> str:
    """Ask for a list long enough that only the output cap can stop it."""
    return (
        f"Output every integer from 1 to {target}, one per line. "
        "Do not add commentary, do not summarize, and do not stop early. "
        "Keep going until the list is complete or the system stops you."
    )


def probe_output(endpoint: str, model: str, target: int, *, timeout: float,
                 max_tokens: int) -> dict:
    prompt = build_output_prompt(target)
    started = time.monotonic()
    try:
        doc = ask_doc(endpoint, model, prompt, timeout=timeout, max_tokens=max_tokens)
        text = "".join(b.get("text", "") for b in (doc.get("content") or [])
                       if isinstance(b, dict))
        usage = doc.get("usage") or {}
        row = {
            "model": model,
            "target_lines": target,
            "output_tokens": usage.get("output_tokens", 0),
            "stop_reason": doc.get("stop_reason"),
            "chars": len(text),
            "lines": len([line for line in text.splitlines() if line.strip()]),
            "elapsed_s": round(time.monotonic() - started, 2),
            "error": "",
        }
    except Exception as exc:  # noqa: BLE001 - a probe reports any failure
        row = {
            "model": model,
            "target_lines": target,
            "output_tokens": 0,
            "stop_reason": "",
            "chars": 0,
            "lines": 0,
            "elapsed_s": round(time.monotonic() - started, 2),
            "error": f"{type(exc).__name__}: {exc}",
        }
    print(
        f"{model:28s} target {target:>8,} lines  "
        f"out {row['output_tokens']:>8,} tok  lines {row['lines']:>8,}  "
        f"stop {row['stop_reason'] or '?':10s} {row['elapsed_s']:7.2f}s"
        + (f"  {row['error']}" if row["error"] else ""),
        flush=True,
    )
    return row


def probe(endpoint: str, model: str, sizes, *, timeout: float, max_tokens: int,
          stop_on_fail: bool) -> list[dict]:
    results = []
    for target in sizes:
        prompt = build_prompt(target)
        approx = estimate_tokens(prompt)
        started = time.monotonic()
        try:
            text = ask(endpoint, model, prompt, timeout=timeout, max_tokens=max_tokens)
            ok = CODE in text
            error = ""
        except urllib.error.HTTPError as exc:
            text = ""
            ok = False
            error = f"HTTP {exc.code}: {exc.read()[:200].decode('utf-8', 'replace')}"
        except Exception as exc:  # noqa: BLE001 - a probe reports any failure
            text = ""
            ok = False
            error = f"{type(exc).__name__}: {exc}"
        elapsed = time.monotonic() - started
        row = {
            "model": model,
            "target_tokens": target,
            "approx_tokens": approx,
            "ok": ok,
            "elapsed_s": round(elapsed, 2),
            "answer": text[:80].replace("\n", " "),
            "error": error,
        }
        results.append(row)
        print(
            f"{model:28s} target {target:>8,}  approx {approx:>8,}  "
            f"{'PASS' if ok else 'FAIL':4s}  {elapsed:7.2f}s"
            + (f"  {error}" if error else ""),
            flush=True,
        )
        if not ok and stop_on_fail:
            break
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Needle-in-a-haystack context probe against an Anthropic endpoint.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--model", required=True)
    ap.add_argument("--mode", choices=("context", "output"), default="context")
    ap.add_argument("--sizes", default=",".join(str(n) for n in DEFAULT_SIZES),
                    help="comma-separated target token counts, ascending")
    ap.add_argument("--target", type=int, default=20000,
                    help="lines to request in output mode")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--keep-going", action="store_true",
                    help="probe larger sizes even after a failure")
    args = ap.parse_args(argv)

    if args.mode == "output":
        row = probe_output(
            args.endpoint, args.model, args.target,
            timeout=args.timeout, max_tokens=args.max_tokens,
        )
        if args.json:
            print(json.dumps(row, indent=2))
        return 0 if not row["error"] else 1

    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    results = probe(
        args.endpoint, args.model, sizes,
        timeout=args.timeout, max_tokens=args.max_tokens,
        stop_on_fail=not args.keep_going,
    )
    if args.json:
        print(json.dumps(results, indent=2))
    passed = [r["target_tokens"] for r in results if r["ok"]]
    if passed:
        print(f"largest passing probe: {max(passed):,} tokens")
        return 0
    print("no probe passed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
