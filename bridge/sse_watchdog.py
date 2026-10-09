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

from sse_finalize import SseCutState, clamp_max_tokens, strip_1m_model  # noqa: E402

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


def _fwd_headers(request: Request) -> dict[str, str]:
    out = {}
    for k, v in request.headers.items():
        if k.lower() not in HOP:
            out[k] = v
    return out


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
) -> AsyncIterator[bytes]:
    st = SseCutState(ping_idle_s=ping_idle_s, first_byte_s=first_byte_s)
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



async def proxy(request: Request) -> Response:
    upstream = request.app.state.upstream
    url = f"{upstream}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    body = await request.body()
    forward_body, stripped_model = strip_1m_model(body)
    clamp_map = getattr(request.app.state, "clamp_map", {}) or {}
    clamp_fallback = getattr(request.app.state, "clamp_fallback", None)
    forward_body, clamped = clamp_max_tokens(forward_body, clamp_map, fallback=clamp_fallback)
    try:
        import json as _j
        _b = _j.loads(forward_body) if forward_body else {}
        if stripped_model:
            print(f"[WATCHDOG] Stripped 1M suffix: {stripped_model} -> {_b.get('model')}", flush=True)
        if clamped is not None:
            print(f"[WATCHDOG] Clamped max_tokens -> {clamped} for model={_b.get('model')}", flush=True)
        print(f"[WATCHDOG] Request: path={request.url.path} model={_b.get('model')} thinking={_b.get('thinking')} max_tokens={_b.get('max_tokens')} body_bytes={len(forward_body)}", flush=True)
    except Exception:
        pass
    timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
    client: httpx.AsyncClient = request.app.state.client
    req = client.build_request(
        request.method,
        url,
        headers=_fwd_headers(request),
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
    if "text/event-stream" in ctype:
        return StreamingResponse(
            _sse_body(
                r,
                ping_idle_s=request.app.state.ping_idle_s,
                first_byte_s=request.app.state.first_byte_s,
            ),
            status_code=r.status_code,
            media_type="text/event-stream",
            headers=out_headers,
        )

    data = await r.aread()
    await r.aclose()
    return Response(
        content=data,
        status_code=r.status_code,
        headers=out_headers,
        media_type=r.headers.get("content-type"),
    )


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
    clamp_map: dict[str, int] | None = None,
    clamp_fallback: int | None = None,
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
    app.state.client = httpx.AsyncClient(timeout=None)
    return app


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4000)
    p.add_argument("--upstream", default="http://127.0.0.1:4001")
    p.add_argument("--idle-seconds", type=float, default=0.0, help="0=never idle-cut (required for long Explore/tool turns)")
    p.add_argument("--first-byte-seconds", type=float, default=90.0)
    p.add_argument("--clamp-map", default=None, help="Path to clamp_map.json (default: bridge/clamp_map.json)")
    p.add_argument("--no-clamp", action="store_true", help="Disable max_tokens clamping")
    args = p.parse_args()
    import uvicorn

    if args.no_clamp:
        cmap, cfallback = {}, None
    else:
        cmap, cfallback = load_clamp_map(args.clamp_map)
    print(f"[WATCHDOG] max_tokens clamp: {len(cmap)} models, fallback={cfallback}", flush=True)

    uvicorn.run(
        build_app(args.upstream, args.idle_seconds, args.first_byte_seconds, cmap, cfallback),
        host=args.host,
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
