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

from sse_finalize import SseCutState  # noqa: E402

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
    try:
        import json as _j
        _b = _j.loads(body) if body else {}
        print(f"[WATCHDOG] Request: path={request.url.path} model={_b.get('model')} thinking={_b.get('thinking')} max_tokens={_b.get('max_tokens')} body_bytes={len(body)}", flush=True)
    except Exception:
        pass
    timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
    client: httpx.AsyncClient = request.app.state.client
    req = client.build_request(
        request.method,
        url,
        headers=_fwd_headers(request),
        content=body or None,
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


def build_app(upstream: str, ping_idle_s: float, first_byte_s: float = 90.0) -> Starlette:
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
    app.state.client = httpx.AsyncClient(timeout=None)
    return app


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4000)
    p.add_argument("--upstream", default="http://127.0.0.1:4001")
    p.add_argument("--idle-seconds", type=float, default=0.0, help="0=never idle-cut (required for long Explore/tool turns)")
    p.add_argument("--first-byte-seconds", type=float, default=90.0)
    args = p.parse_args()
    import uvicorn

    uvicorn.run(
        build_app(args.upstream, args.idle_seconds, args.first_byte_seconds),
        host=args.host,
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
