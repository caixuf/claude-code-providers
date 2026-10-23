#!/usr/bin/env python3
"""Re-derive which models a Qianfan Token-Plan key can actually call.

`/v2/models` advertises ~35 ids, but a Token Plan Person key only serves the
GLM / DeepSeek families — everything else answers HTTP 403
``token_plan_person_model_not_supported``.  The allow-list in
``bridge/discover_models.py`` is therefore a property of *the subscription*,
not of the provider: a new key can serve a different subset, and the old
allow-list would advertise models the key cannot call.

Usage:

    python3 bridge/qianfan_probe.py                     # probe, print the set
    python3 bridge/qianfan_probe.py --emit-only-models  # paste into PROVIDERS

The key is read from ``~/.config/litellm/proxy.env`` (``QIANFAN_API_KEY``),
which is where the bridge service reads it from too.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://qianfan.baidubce.com"
CATALOGUE_URL = f"{BASE}/v2/models"
CHAT_URL = f"{BASE}/v2/tokenplan/personal/chat/completions"
PROXY_ENV = Path(os.path.expanduser("~/.config/litellm/proxy.env"))

# The catalogue and the plan endpoint both answer 429 under a burst of
# probes. The catalogue says "Too Many Requests"; the chat endpoint says
# "Token Plan Person monthly quota limit exceeded" — which reads like a dead
# plan but is not: the same key answered every id minutes later, once the
# burst had passed. So every call retries with a growing backoff instead of
# concluding the plan is gone.
RATE_LIMIT_BACKOFF = (2, 4, 8)


def read_key() -> str:
    """The key from proxy.env, so the probe uses exactly what the bridge uses."""
    if not PROXY_ENV.is_file():
        sys.exit(f"no {PROXY_ENV}")
    for line in PROXY_ENV.read_text().splitlines():
        m = re.match(r"^\s*QIANFAN_API_KEY\s*=\s*(.+?)\s*$", line)
        if m:
            return m.group(1).strip().strip("\"'")
    sys.exit(f"QIANFAN_API_KEY not found in {PROXY_ENV}")


def call(url: str, payload: dict | None = None, tries: int = 4):
    """One HTTP call; retries only on the transient 429."""
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": f"Bearer {KEY}"}
    if data:
        headers["Content-Type"] = "application/json"
    last = (0, {"message": "not attempted"})
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = {"message": body[:200]}
            if e.code == 429 and attempt < tries - 1:
                time.sleep(RATE_LIMIT_BACKOFF[min(attempt, len(RATE_LIMIT_BACKOFF) - 1)])
                last = (e.code, parsed)
                continue
            return e.code, parsed
        except Exception as exc:  # noqa: BLE001 - a network hiccup, not a verdict
            last = (0, {"message": repr(exc)})
            if attempt < tries - 1:
                time.sleep(2)
                continue
            return last
    return last


def main() -> None:
    global KEY

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", default=None, help="override the key from proxy.env")
    ap.add_argument("--emit-only-models", action="store_true",
                    help="print the allow-list block for discover_models.py")
    ap.add_argument("--sleep", type=float, default=0.6,
                    help="pause between probes (default 0.6s)")
    args = ap.parse_args()

    KEY = args.key or read_key()

    status, cat = call(CATALOGUE_URL)
    if status != 200:
        sys.exit(f"catalogue failed: HTTP {status} {cat}")
    ids = [m.get("id") for m in cat.get("data", []) if m.get("id")]
    print(f"catalogue: {len(ids)} ids\n")

    supported, unsupported = [], []
    for mid in ids:
        st, body = call(CHAT_URL, {
            "model": mid,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8,
        })
        if st == 200:
            supported.append(mid)
            print(f"  OK   {mid}")
        else:
            err = body.get("error") if isinstance(body.get("error"), dict) else body
            code = (err or {}).get("code") or (err or {}).get("message") or ""
            unsupported.append((mid, st, code))
            print(f"  FAIL {mid} -> HTTP {st} {code}")
        time.sleep(args.sleep)

    print(f"\nsupported ({len(supported)}):")
    for m in supported:
        print(f"  {m}")
    print(f"unsupported ({len(unsupported)})")

    if args.emit_only_models:
        print("\n        \"only_models\": [")
        for m in supported:
            print(f"            \"{m}\",")
        print("        ],")


if __name__ == "__main__":
    main()
