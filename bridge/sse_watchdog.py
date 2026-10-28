#!/usr/bin/env python3
"""HTTP reverse proxy in front of LiteLLM: close Anthropic SSE after message_stop.

LiteLLM stays on 127.0.0.1:4001. This process listens on 127.0.0.1:4000
(the URL Claude Code already uses).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import AsyncIterator

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

_BRIDGE_DIR = Path(__file__).resolve().parent
if str(_BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(_BRIDGE_DIR))

from sse_finalize import (  # noqa: E402
    SseCutState,
    clamp_max_tokens,
    downgrade_media,
    event_is_ping,
    norm_model_key,
    sanitize_channel_markers,
    strip_1m_beta,
    strip_1m_model,
)
from bridge_auth import load_master_key, normalize_auth  # noqa: E402
from loopback import ensure_loopback_bypass, loopback_mounts  # noqa: E402
from usage_ledger import UsageLedger, UsageTally  # noqa: E402

HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def _fwd_headers(
    request: Request, master_key: str | None = None
) -> tuple[dict[str, str], str]:
    out = {}
    for k, v in request.headers.items():
        if k.lower() in HOP:
            continue
        # Drop the 1M-context beta token: [1m] markers add it, but the
        # OpenAI-protocol gateways behind the bridge don't understand it and
        # a strict one can 400. The 1M window is a property of the model here,
        # not of this Anthropic beta.
        if k.lower() == "anthropic-beta":
            v = strip_1m_beta(v)
            if not v:
                continue
        out[k] = v
    # LiteLLM accepts one credential: the master key in the config it was
    # started with. A client that carries a stale one gets "No connected
    # db." 400, and a client that carries none gets a 500 out of LiteLLM's
    # own error handler. Both are the client's mistake, and neither may stop
    # a call: the bridge forwards the key LiteLLM actually accepts.
    out, note = normalize_auth(out, master_key)
    return out, note


async def _drain_upstream(aiter, resp: httpx.Response) -> None:
    try:
        async for _ in aiter:
            pass
    except Exception:
        pass
    finally:
        try:
            await resp.aclose()
        except Exception:
            pass


async def _sse_body(
    resp: httpx.Response,
    *,
    ping_idle_s: float,
    first_byte_s: float,
    on_close=None,
) -> AsyncIterator[bytes]:
    import time as _time

    st = SseCutState(ping_idle_s=ping_idle_s, first_byte_s=first_byte_s)
    # The tally rides along on the same chunk walk the cut logic already does,
    # so usage costs no extra buffering and survives a cut: the events that
    # carry the counts (`message_start`, `message_delta`) arrive before
    # `message_stop`, which is where we stop forwarding.
    tally = UsageTally()
    started = _time.monotonic()
    ttft_ms: int | None = None
    aiter = resp.aiter_bytes()
    handed_off = False
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(aiter.__anext__(), timeout=st.wait_budget())
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            tally.feed(chunk)
            if ttft_ms is None and not event_is_ping(chunk):
                ttft_ms = int((_time.monotonic() - started) * 1000)
            out, done = st.feed(chunk)
            if out:
                yield out
            if done:
                asyncio.create_task(_drain_upstream(aiter, resp))
                handed_off = True
                break
    finally:
        if not handed_off:
            await resp.aclose()
        # Records even on a cut or a client disconnect: the tokens were billed
        # upstream whether or not the rest of the stream was read.
        if on_close is not None:
            try:
                on_close(tally, ttft_ms, int((_time.monotonic() - started) * 1000))
            except Exception:
                pass


async def proxy(request: Request) -> Response:
    upstream = request.app.state.upstream
    url = f"{upstream}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    body = await request.body()
    forward_body, stripped_model = strip_1m_model(body)
    clamp_map = getattr(request.app.state, "clamp_map", {}) or {}
    clamp_fallback = getattr(request.app.state, "clamp_fallback", None)
    rules = getattr(request.app.state, "request_rules", None) or {}
    sanitize_models = rules.get("sanitize_channel") or frozenset()
    text_only_models = rules.get("media_downgrade") or frozenset()
    try:
        import json as _j
        _sent_model = (_j.loads(forward_body) or {}).get("model", "") if forward_body else ""
    except Exception:
        _sent_model = ""
    channel_rewrites: list[str] = []
    if sanitize_models and norm_model_key(_sent_model) in sanitize_models:
        forward_body, channel_rewrites = sanitize_channel_markers(forward_body)
    # A text-only upstream refuses the whole request when it meets an image
    # (Qianfan: 400 "image_url is only supported by certain models"), and the
    # image is already in the durable history, so every later turn of that
    # session would fail the same way. Replacing the pixels with a marker keeps
    # the session alive; which models need it comes from the registry's
    # discovered `vision` flag via bridge/request_rules.json.
    media_dropped = 0
    if text_only_models and norm_model_key(_sent_model) in text_only_models:
        forward_body, media_dropped = downgrade_media(forward_body, _sent_model)
    forward_body, clamped = clamp_max_tokens(forward_body, clamp_map, fallback=clamp_fallback)
    try:
        import json as _j
        _b = _j.loads(forward_body) if forward_body else {}
        if stripped_model:
            print(f"[WATCHDOG] Stripped 1M suffix: {stripped_model} -> {_b.get('model')}", flush=True)
        if channel_rewrites:
            print(f"[WATCHDOG] Channel rewrites ({', '.join(channel_rewrites)}) for model={_b.get('model')}", flush=True)
        if media_dropped:
            print(f"[WATCHDOG] Dropped {media_dropped} media block(s) for text-only model={_b.get('model')}", flush=True)
        if clamped is not None:
            print(f"[WATCHDOG] Clamped max_tokens -> {clamped} for model={_b.get('model')}", flush=True)
        auth_bit = f" auth={auth_note}" if auth_note else ""
        print(f"[WATCHDOG] Request: path={request.url.path} model={_b.get('model')} thinking={_b.get('thinking')} max_tokens={_b.get('max_tokens')} body_bytes={len(forward_body)}{auth_bit}", flush=True)
    except Exception:
        pass
    timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
    client: httpx.AsyncClient = request.app.state.client
    # The channel's three conversation headers are a deployment-level
    # `extra_headers` in the registry, which LiteLLM adds — not the watchdog's
    # job. See registry/models.yaml and render_config.py.
    fwd_headers, auth_note = _fwd_headers(
        request, getattr(request.app.state, "master_key", None)
    )
    req = client.build_request(
        request.method,
        url,
        headers=fwd_headers,
        content=forward_body or None,
        timeout=timeout,
    )
    r = None
    for attempt in range(4):
        try:
            r = await client.send(req, stream=True)
            break
        except httpx.ConnectError:
            if attempt == 3:
                raise
            await asyncio.sleep(0.5)
    ctype = (r.headers.get("content-type") or "").lower()
    out_headers = {
        k: v
        for k, v in r.headers.items()
        if k.lower() not in HOP and k.lower() != "content-type"
    }
    ledger = getattr(request.app.state, "ledger", None)
    sent_model = _sent_model or "unknown"

    def _record(tally, ttft_ms, duration_ms, status, streamed):
        if ledger is None:
            return
        try:
            ledger.record(
                provider=getattr(request.app.state, "provider", "") or "",
                model=tally.model or sent_model,
                usage=tally.usage,
                status=status,
                duration_ms=duration_ms,
                streamed=streamed,
                ttft_ms=ttft_ms,
            )
        except Exception:
            pass

    if "text/event-stream" in ctype:
        return StreamingResponse(
            _sse_body(
                r,
                ping_idle_s=request.app.state.ping_idle_s,
                first_byte_s=request.app.state.first_byte_s,
                on_close=lambda tally, ttft, dur: _record(tally, ttft, dur, r.status_code, True),
            ),
            status_code=r.status_code,
            media_type="text/event-stream",
            headers=out_headers,
        )

    import time as _time

    _t0 = _time.monotonic()
    data = await r.aread()
    await r.aclose()
    if r.status_code < 400:
        # `/health`, `/v1/models` and friends share this port and carry no
        # usage; only a body that actually has counters is a billed request.
        tally = UsageTally()
        tally.absorb_json(data)
        if not tally.usage.is_empty():
            _record(tally, None, int((_time.monotonic() - _t0) * 1000), r.status_code, False)
    return Response(
        content=data,
        status_code=r.status_code,
        headers=out_headers,
        media_type=r.headers.get("content-type"),
    )


def load_request_rules(path: str | None) -> dict[str, set[str]]:
    """Load per-model request rewrites from ``bridge/request_rules.json``.

    Shape: ``{"sanitize_channel": ["codebuddy", "codebuddy-glm", ...]}``.
    Model names are normalized (``[1m]`` stripped, lower-cased) on both sides so
    a rule written once covers the bracketed spelling the client actually sends.
    A missing or malformed file yields ``{}`` — rules are an optimisation for
    picky gateways, never a precondition for proxying.
    """
    import json
    from pathlib import Path

    candidates = [path] if path else []
    candidates.append(str(_BRIDGE_DIR / "request_rules.json"))
    for cand in candidates:
        if not cand:
            continue
        fp = Path(cand)
        if not fp.is_file():
            continue
        try:
            raw = json.loads(fp.read_text())
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        out: dict[str, set[str]] = {}
        for rule, names in raw.items():
            if isinstance(names, list):
                out[str(rule)] = {norm_model_key(n) for n in names if isinstance(n, str)}
        return out
    return {}


def load_clamp_map(path: str | None) -> tuple[dict[str, int], int | None]:
    """Load {model: max_output} from a JSON file. Returns ({}, None) if missing.

    The fallback cap is the minimum across all models, so an unknown model is
    still clamped to the most conservative known output limit.
    """
    import json
    from pathlib import Path

    candidates = [path] if path else []
    candidates.append(str(_BRIDGE_DIR / "clamp_map.json"))
    for cand in candidates:
        if not cand:
            continue
        fp = Path(cand)
        if not fp.is_file():
            continue
        try:
            raw = json.loads(fp.read_text())
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        clean = {str(k): int(v) for k, v in raw.items() if isinstance(v, (int, float))}
        fallback = clean.pop("__fallback__", None)
        if fallback is None and clean:
            fallback = min(clean.values())
        return clean, fallback
    return {}, None


def build_app(
    upstream: str,
    ping_idle_s: float,
    first_byte_s: float = 90.0,
    usage_path: str | None = None,
    clamp_map: dict[str, int] | None = None,
    clamp_fallback: int | None = None,
    request_rules: dict[str, set[str]] | None = None,
    master_key: str | None = None,
) -> Starlette:
    methods = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
    app = Starlette(
        routes=[
            Route("/", proxy, methods=methods),
            Route("/{path:path}", proxy, methods=methods),
        ]
    )
    app.state.upstream = upstream.rstrip("/")
    app.state.ping_idle_s = ping_idle_s
    app.state.first_byte_s = first_byte_s
    app.state.clamp_map = clamp_map or {}
    app.state.clamp_fallback = clamp_fallback
    app.state.request_rules = request_rules or {}
    # The credential the bridge forwards: LiteLLM accepts only this one.
    app.state.master_key = master_key
    # The ledger is optional: --no-usage keeps the proxy byte-identical to what
    # it was, for anyone who would rather not write a file per request.
    app.state.ledger = UsageLedger(usage_path) if usage_path else None
    # The upstream is LiteLLM on this machine. A request to 127.0.0.1 that
    # the HTTP proxy answers for comes back 502 with an empty body, and the
    # bridge then looks broken while both processes are perfectly healthy.
    # The proxy env a service inherits is not the one a shell has, so the
    # bypass is enforced here.
    ensure_loopback_bypass()
    app.state.client = httpx.AsyncClient(timeout=None, mounts=loopback_mounts())
    return app


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4000)
    p.add_argument("--upstream", default="http://127.0.0.1:4001")
    p.add_argument("--idle-seconds", type=float, default=0.0, help="0=never idle-cut (required for long Explore/tool turns)")
    p.add_argument("--first-byte-seconds", type=float, default=90.0)
    p.add_argument("--clamp-map", default=None, help="Path to clamp_map.json (default: bridge/clamp_map.json)")
    p.add_argument("--request-rules", default=None, help="Path to request_rules.json (default: bridge/request_rules.json)")
    p.add_argument("--no-clamp", action="store_true", help="Disable max_tokens clamping")
    p.add_argument(
        "--litellm-config",
        default=None,
        help="the LiteLLM config the bridge credential is read from (default: ~/.config/litellm/config.yaml)",
    )
    p.add_argument(
        "--usage-path",
        default=str(Path.home() / ".local/state/ccp/usage.jsonl"),
        help="Per-request token ledger (default: ~/.local/state/ccp/usage.jsonl)",
    )
    p.add_argument("--no-usage", action="store_true", help="Do not record token usage")
    args = p.parse_args()
    import uvicorn

    if args.no_clamp:
        cmap, cfallback = {}, None
    else:
        cmap, cfallback = load_clamp_map(args.clamp_map)
    rules = load_request_rules(args.request_rules)
    master_key, key_source = load_master_key(args.litellm_config)
    print(f"[WATCHDOG] bridge credential: read from {key_source}", flush=True)
    print(f"[WATCHDOG] max_tokens clamp: {len(cmap)} models, fallback={cfallback}", flush=True)
    print(
        "[WATCHDOG] request rules: "
        + (", ".join(f"{k}={len(v)} models" for k, v in sorted(rules.items())) or "none"),
        flush=True,
    )

    usage_path = None if args.no_usage else args.usage_path
    print(
        f"[WATCHDOG] usage ledger: {usage_path or 'off'}",
        flush=True,
    )

    uvicorn.run(
        build_app(
            args.upstream,
            args.idle_seconds,
            args.first_byte_seconds,
            usage_path=usage_path,
            clamp_map=cmap,
            clamp_fallback=cfallback,
            request_rules=rules,
            master_key=master_key,
        ),
        host=args.host,
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
