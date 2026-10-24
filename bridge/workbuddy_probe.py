#!/usr/bin/env python3
"""Re-derive what a WorkBuddy / CodeBuddy plan can actually call, and see.

Two things about this plan cannot be read from its catalogue:

* **Which ids it serves.** ``/v2/enterprises/personal/models`` is the account's
  own catalogue, but it is wider than the plan: on a ``personal_free_user``
  account the GPT and Gemini rungs answer ``model [x] is not allowed for user
  type [personal_free_user]``, and some ids answer ``service info not found``.
  It also omits the concrete ids the CLI itself asks for (``deepseek-v4.1-flash``).
* **Which of them can see an image.** The catalogue publishes
  ``supportsImages`` per model, and on this plan it is *wrong*: it says
  ``glm-5.3`` and ``glm-5.2`` take images, and those two answer "I'm not able to
  view images — the current model doesn't support image input". A model that
  says so is text-only whatever the catalogue claims, and Claude Code needs the
  truth here, because it decides whether a pasted screenshot reaches the model
  or is silently replaced by a placeholder.

So this probes: it calls every candidate with a one-word prompt, and then calls
each one that answers with a generated solid-green PNG and reads what comes
back. A model that says "green" sees images; a model that says it cannot, or
answers nothing, does not.

Usage:

    python3 bridge/workbuddy_probe.py                     # probe, print the sets
    python3 bridge/workbuddy_probe.py --emit-only-models  # the allow-list to paste
    python3 bridge/workbuddy_probe.py --emit-hints        # the vision hints to paste

The token is read from the CodeBuddy Code session (the same one
``ccp auth workbuddy`` imports) or from ``~/.config/litellm/proxy.env``.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path

INTL_BASE = "https://www.codebuddy.ai/v2"
CN_BASE = "https://copilot.tencent.com/v2"
SESSION_DIR = "~/.local/share/CodeBuddyExtension/Data/Public/auth"
PROXY_ENV = Path(os.path.expanduser("~/.config/litellm/proxy.env"))

# The gateway's channel check: without these three headers a request is
# refused with 400 11128 "request illegal" whatever else it looks like. The
# values are not validated. docs/workbuddy-channel-check.md has the capture.
CONVERSATION_HEADERS = {
    "X-Conversation-ID": "ccp-workbuddy-probe",
    "X-Conversation-Message-ID": "ccp-workbuddy-probe",
    "X-Conversation-Request-ID": "ccp-workbuddy-probe",
}

# Ids worth asking about: the account's own catalogue plus the concrete names
# the CLI asks for, which the catalogue does not list.
EXTRA_CANDIDATES = [
    "auto",
    "deepseek-v4.1-flash",
    "glm-5.1",
    "glm-5.3-flash",
    "glm-5v-turbo",
    "hy4-preview-f",
    "kimi-k2.7",
    "minimax-m3",
]

RATE_LIMIT_BACKOFF = (2, 4, 8)


def read_token() -> tuple[str, str]:
    """(token, base) — from the CLI session, else from proxy.env."""
    for path in sorted(Path(os.path.expanduser(SESSION_DIR)).glob("*.info"), reverse=True):
        try:
            doc = json.loads(path.read_text())
        except Exception:
            continue
        auth = doc.get("auth") or {}
        token = auth.get("accessToken")
        if not isinstance(token, str) or not token:
            continue
        domain = auth.get("domain") or ""
        base = CN_BASE if "tencent.com" in domain or domain.endswith(".cn") else INTL_BASE
        return token, base
    if PROXY_ENV.is_file():
        token = ""
        base = INTL_BASE
        for line in PROXY_ENV.read_text().splitlines():
            if line.startswith("WORKBUDDY_API_KEY="):
                token = line.split("=", 1)[1].strip().strip("\"'")
            if line.startswith("WORKBUDDY_API_BASE="):
                base = line.split("=", 1)[1].strip().strip("\"'")
        if token:
            return token, base
    sys.exit("no WorkBuddy session found — log in with `codebuddy` and run `ccp auth workbuddy`")


def green_png_data_url(width: int = 16, height: int = 16) -> str:
    """A solid green PNG, so "what colour is this?" has one right answer."""
    raw = b"".join(b"\x00" + bytes((0, 200, 0)) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(png).decode()


def call(base: str, token: str, payload: dict, tries: int = 4) -> tuple[int, str]:
    """One chat completion; retries only on a transient 429.

    The plan answers 429 under a burst of probes, and the body it gives then
    reads like a dead plan ("monthly quota limit exceeded") when it is only a
    rate limit — the same key answers minutes later. So a 429 is a pause, not
    a verdict.
    """
    data = json.dumps(payload).encode()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        **CONVERSATION_HEADERS,
    }
    last = (0, "")
    for attempt in range(tries):
        req = urllib.request.Request(base + "/chat/completions", data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                return resp.status, resp.read(4000).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = e.read(600).decode("utf-8", "replace")
            if e.code == 429 and attempt < tries - 1:
                time.sleep(RATE_LIMIT_BACKOFF[min(attempt, len(RATE_LIMIT_BACKOFF) - 1)])
                last = (e.code, body)
                continue
            return e.code, body
        except Exception as exc:  # noqa: BLE001 - a network hiccup, not a verdict
            last = (0, repr(exc))
            if attempt < tries - 1:
                time.sleep(2)
                continue
            return last
    return last


def text_of(stream: str) -> str:
    """The visible text of an SSE reply (the reasoning is a different field)."""
    return "".join(re.findall(r'"content":"([^"]*)"', stream))


def serves(base: str, token: str, model: str) -> tuple[bool, str]:
    status, body = call(base, token, {
        "model": model, "stream": True, "max_tokens": 8,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Say OK."},
        ],
    })
    if status == 200:
        return True, ""
    try:
        err = (json.loads(body).get("error") or {})
        return False, str(err.get("code") or err.get("message") or body[:80])
    except Exception:
        return False, body[:80]


def sees_images(base: str, token: str, model: str) -> bool | None:
    """True if it names the colour, False if it says it cannot, None if quiet.

    A reasoning model can spend the whole token budget thinking and answer
    nothing, so an empty reply is *unknown*, not "text only" — it is asked
    again with a bigger budget before it is believed.
    """
    for max_tokens in (60, 400):
        status, body = call(base, token, {
            "model": model, "stream": True, "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": [
                    {"type": "text", "text": "Look at the image. What colour is it? Answer with one lowercase word."},
                    {"type": "image_url", "image_url": {"url": green_png_data_url()}},
                ]},
            ],
        })
        if status != 200:
            return None
        said = text_of(body).lower()
        if "green" in said:
            return True
        if said.strip():
            # it answered and the answer was not the colour
            return False
        # silent: it may have spent the budget thinking, so try again with more
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--emit-only-models", action="store_true", help="print the allow-list block")
    ap.add_argument("--emit-hints", action="store_true", help="print the vision hints block")
    ap.add_argument("--sleep", type=float, default=0.6, help="pause between probes")
    ap.add_argument("--skip-vision", action="store_true", help="only test which ids answer")
    args = ap.parse_args()

    token, base = read_token()
    print(f"gateway: {base}")

    catalogue: list[str] = []
    try:
        req = urllib.request.Request(
            base + "/enterprises/personal/models",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            doc = json.loads(resp.read().decode())
        catalogue = [m["id"] for m in (doc.get("data") or {}).get("models", []) if m.get("id")]
    except Exception as exc:  # noqa: BLE001
        print(f"catalogue unavailable: {exc}")
    print(f"catalogue: {len(catalogue)} ids\n")

    candidates = list(dict.fromkeys(catalogue + EXTRA_CANDIDATES))
    serving: list[str] = []
    vision: dict[str, bool] = {}
    for model in candidates:
        ok, why = serves(base, token, model)
        if not ok:
            print(f"  FAIL {model:<22} {why}")
            time.sleep(args.sleep)
            continue
        print(f"  OK   {model:<22}", end="", flush=True)
        if not args.skip_vision:
            sees = sees_images(base, token, model)
            if sees is None:
                print("  vision: unknown", flush=True)
            else:
                vision[model] = sees
                print(f"  vision: {'yes' if sees else 'no'}", flush=True)
        else:
            print()
        serving.append(model)
        time.sleep(args.sleep)

    print(f"\nserving ({len(serving)}): {serving}")
    if vision:
        yes = [m for m, v in vision.items() if v]
        no = [m for m, v in vision.items() if not v]
        print(f"sees images ({len(yes)}): {yes}")
        print(f"text only  ({len(no)}): {no}")

    if args.emit_only_models:
        print('\n        "only_models": [')
        for m in sorted(serving):
            print(f'            "{m}",')
        print("        ],")
    if args.emit_hints:
        print('\n        "hints": {')
        for m in sorted(vision):
            print(f'            "{m}": {{"vision": {str(vision[m]).lower()}}},')
        print("        },")


if __name__ == "__main__":
    main()
