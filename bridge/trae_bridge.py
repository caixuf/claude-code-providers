#!/usr/bin/env python3
"""trae_bridge.py — expose the Trae CLI subscription as an Anthropic endpoint.

TraeCLI (ByteDance's enterprise coding agent) publishes no public API. It
authenticates with a personal access token and then runs every model turn
through one private, streaming RPC:

    POST https://api.enterprise.trae.cn/api/ide/v2/llm_raw_chat

That call is the only way to reach the models the subscription pays for
(GLM-5.3, Kimi-K3, MiniMax-M3, Doubao-Seed, DeepSeek-V4.1…), so this file
replays it and re-shapes the result into the Anthropic Messages API that Claude
Code speaks.

Why an adapter instead of LiteLLM
---------------------------------
Every other provider in ccp is either Anthropic-native or OpenAI-compatible, so
LiteLLM can front it. Trae is neither: the request carries a bespoke ``Extra``
header, ``config_name``/``model_name`` are Trae catalogue ids rather than model
names, and the response is an SSE dialect (``event: output`` carrying a
``tool_calls`` array) that no LiteLLM provider understands. Translating here
keeps LiteLLM out of the path entirely, so this provider needs no bridge, no
:4000 hop, and no change to the running LiteLLM/watchdog deployment.

Protocol notes (all verified against the live service)
-----------------------------------------------------
* Auth is two-step: the PAT in ``TRAECLI_PERSONAL_ACCESS_TOKEN`` is exchanged
  for a JWT via ``/cloudide/api/v3/trae/oauth/ExchangeToken``. The JWT lasts 14
  days; the PAT does not expire, so the exchange just runs at startup and again
  when the JWT is near expiry.
* ``tools[].function.parameters`` must be a **JSON-encoded string**. Trae
  answers a bare HTTP 500 — with the literal body ``Internal Server Error`` and
  nothing else — if you pass an object. This is the single most expensive
  detail in the protocol to rediscover, so it is asserted in the tests.
* Streaming deltas arrive as ``event: output`` holding ``response`` (text),
  ``reasoning_content`` (thinking) and ``tool_calls`` (indexed function calls
  whose ``arguments`` stream in fragments).
* ``event: done`` carries ``finish_reason``; ``event: token_usage`` carries the
  counts; ``event: metadata`` names the real upstream model.
* An empty ``response`` with populated ``reasoning_content`` is normal — Trae
  streams thinking before content.
* A turn that is purely a tool call still carries ``content: " "`` (a single
  space). Reproducing that matters: it is what makes the follow-up tool_result
  turn valid.

Limitations (v1)
----------------
* Images are not translated. Trae takes them in a separate
  ``multimodal_contents`` field whose exact shape is not yet captured, so an
  ``image`` block is dropped rather than sent malformed.
* ``max_tokens`` / ``temperature`` are accepted and ignored: Trae chooses the
  output cap from its own catalogue entry.

Usage:
  python3 trae_bridge.py --check              # validate auth, print identity
  python3 trae_bridge.py --list-models        # what the subscription exposes
  python3 trae_bridge.py --serve [--port N]   # run the Anthropic endpoint
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TRAE_HOST = "https://api.enterprise.trae.cn"
DEFAULT_PORT = 4100
# The CLI ships this app id; the gateway rejects requests without it.
TRAE_APP_ID = "7b3f9dc2-8a4e-5c6d-2f1b-9e4a3c5b7df0"
TRAE_IDE_VERSION = "99.99.99"
TRAE_IDE_VERSION_CODE = "20260206"

# Re-exchange the PAT this long before the JWT actually lapses, so a long turn
# never starts on a token that expires mid-stream.
JWT_RENEW_MARGIN_MS = 10 * 60 * 1000

# The catalogue changes rarely; TraeCLI itself fetches it once per process.
CATALOGUE_TTL_S = 600


def _opener() -> urllib.request.OpenerDirector:
    """An opener that honours the ambient proxy *and* ``no_proxy``.

    ``build_opener()`` with no arguments installs the default ProxyHandler,
    which reads ``HTTPS_PROXY``/``NO_PROXY`` at construction time and calls
    ``proxy_bypass`` per request. That is exactly what Go's client does for
    TraeCLI, so both agree on how this host is reached.
    """
    return urllib.request.build_opener()


# Rows Trae lists in its own catalogue that are not models the subscription
# serves. `summary` backs Trae's own history compaction and
# `custom_model_placeholder` is the slot for bring-your-own-key models; both
# are hidden from Trae's /model picker. They carry a model_name, so filtering
# on that alone lets them into the registry as if they were pickable.
_TRAE_INTERNAL_ROWS = ("summary", "custom_model_placeholder")


def _catalogue_from(payload: dict) -> list[dict]:
    """Turn a get_config_list response into the models this subscription serves."""
    out = []
    for entry in payload.get("config_info_list") or []:
        details = (entry.get("model_detail_list") or [{}])[0]
        if not details.get("model_name"):
            continue  # no routable backend behind this row
        if entry["config_name"] in _TRAE_INTERNAL_ROWS:
            continue
        display = entry.get("display_config") or {}
        out.append({
            "config_name": entry["config_name"],
            "model_name": details["model_name"],
            "display_name": display.get("display_name") or entry["config_name"],
            "ctx": details.get("prompt_max_tokens") or 128000,
            "max_output": details.get("max_tokens") or 8192,
            "vision": bool(display.get("multimodal")),
        })
    return out


class TraeAuth:
    """Owns the PAT -> JWT exchange, its 14-day lifetime, and the catalogue."""

    def __init__(self, pat: str | None = None, verbose: bool = False):
        self.pat = pat or os.environ.get("TRAECLI_PERSONAL_ACCESS_TOKEN", "")
        self.verbose = verbose
        self._jwt: str | None = None
        self._expires_ms = 0
        self._lock = threading.Lock()
        self._catalogue: list[dict] | None = None
        self._catalogue_at = 0.0
        self._opener = _opener()
        self.identity: dict = {}
        if not self.pat:
            raise SystemExit(
                "TRAECLI_PERSONAL_ACCESS_TOKEN is not set.\n"
                "TraeCLI writes it to ~/.bashrc once you have logged in; run\n"
                "  ccp auth trae\n"
                "to copy it into the bridge's secrets file."
            )

    def _log(self, msg: str) -> None:
        if self.verbose:
            print("[trae] " + msg, file=sys.stderr, flush=True)

    @staticmethod
    def logid() -> str:
        return "%019d" % int(time.time() * 1000) + uuid.uuid4().hex

    def _post(self, path: str, body: dict, headers: dict, timeout: int = 300):
        req = urllib.request.Request(TRAE_HOST + path,
                                     data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        for k, v in headers.items():
            req.add_header(k, v)
        return self._opener.open(req, timeout=timeout)

    def jwt(self, force: bool = False) -> str:
        """Return a valid JWT, exchanging the PAT when needed."""
        with self._lock:
            now = int(time.time() * 1000)
            if not force and self._jwt and now < self._expires_ms - JWT_RENEW_MARGIN_MS:
                return self._jwt
            logid = self.logid()
            try:
                resp = self._post(
                    "/cloudide/api/v3/trae/oauth/ExchangeToken",
                    {"RefreshToken": self.pat},
                    {"X-Client-Request-Id": logid, "X-Tt-Logid": logid},
                )
                payload = json.load(resp)
            except urllib.error.HTTPError as e:
                raise SystemExit(
                    "Trae rejected the personal access token (HTTP %d): %s"
                    % (e.code, e.read()[:300].decode("utf-8", "replace"))) from e
            if payload.get("code") != 0:
                raise SystemExit("Trae token exchange failed: %s"
                                 % json.dumps(payload)[:300])
            data = payload["Data"]
            self._jwt = data["Token"]
            self._expires_ms = int(data["TokenExpireAt"])
            self._log("jwt refreshed, valid for %.1fh"
                      % ((self._expires_ms - now) / 3600000))
            if not self.identity:
                self._fetch_identity()
            return self._jwt

    def _fetch_identity(self) -> None:
        """Best-effort: who the subscription belongs to (cosmetic)."""
        try:
            logid = self.logid()
            resp = self._post("/cloudide/api/v3/trae/GetUserInfo", {},
                              {"Authorization": "Cloud-IDE-JWT " + (self._jwt or ""),
                               "X-Cloudide-Token": self._jwt or "",
                               "X-Client-Request-Id": logid, "X-Tt-Logid": logid})
            self.identity = json.load(resp)
        except Exception as e:
            self._log("identity lookup skipped: %r" % (e,))

    def rpc(self, path: str, body: dict, timeout: int = 300):
        """POST a Trae RPC with a fresh JWT and the app-identity headers."""
        tok = self.jwt()
        logid = self.logid()
        return self._post(path, body, {
            "Authorization": "Cloud-IDE-JWT " + tok,
            "X-App-Id": TRAE_APP_ID,
            "X-Client-Request-Id": logid,
            "X-Ide-Version": TRAE_IDE_VERSION,
            "X-Ide-Version-Code": TRAE_IDE_VERSION_CODE,
            "X-Ide-Function": "chat",
            "X-Tt-Logid": logid,
        }, timeout=timeout)

    def catalogue(self, refresh: bool = False) -> list[dict]:
        """The models this subscription may call, with their real limits."""
        with self._lock:
            fresh = (self._catalogue is not None
                     and time.time() - self._catalogue_at < CATALOGUE_TTL_S)
            if fresh and not refresh:
                return self._catalogue
        payload = json.load(self.rpc("/api/ide/v1/cli/get_config_list",
                                     {"function": "chat"}))
        out = _catalogue_from(payload)
        with self._lock:
            self._catalogue = out
            self._catalogue_at = time.time()
        return out


# ── Trae <-> Anthropic translation ────────────────────────────────────────

def _text_of(content) -> str:
    """Flatten an Anthropic content field (string or block list) to text."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            btype = block.get("type")
            if btype == "text":
                parts.append(block.get("text") or "")
            elif btype == "tool_result":
                parts.append(_text_of(block.get("content")))
            # image / document blocks are dropped (see "Limitations")
    return "".join(parts)


def anthropic_to_trae(messages: list[dict], system) -> list[dict]:
    """Anthropic Messages -> Trae's message list.

    Trae takes OpenAI-shaped roles but Anthropic-shaped content: every message
    carries ``content: [{type: text, text: ...}]``. Tool calls ride on the
    assistant turn as ``tool_calls`` (each with ``function_call``, singular)
    and tool output comes back as ``role: tool`` plus ``tool_call_id``.
    """
    out: list[dict] = []
    sys_text = _text_of(system)
    if sys_text.strip():
        out.append({"role": "system", "content": [{"type": "text", "text": sys_text}]})

    for msg in messages or []:
        role, content = msg.get("role"), msg.get("content")

        if role == "assistant":
            texts, calls = [], []
            if isinstance(content, str):
                texts.append(content)
            for block in content or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    texts.append(block.get("text") or "")
                elif block.get("type") == "tool_use":
                    calls.append({
                        "index": len(calls),
                        "id": block.get("id"),
                        "type": "function",
                        "function_call": {
                            "name": block.get("name") or "",
                            "arguments": json.dumps(block.get("input") or {}),
                        },
                    })
            entry: dict = {
                "role": "assistant",
                # Trae sends a single space when a turn is purely a tool call.
                "content": [{"type": "text", "text": "".join(texts) or " "}],
            }
            if calls:
                entry["tool_calls"] = calls
            out.append(entry)

        elif role == "user":
            blocks = content if isinstance(content, list) else [content]
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    out.append({
                        "role": "tool",
                        "content": [{"type": "text",
                                     "text": _text_of(block.get("content"))}],
                        "tool_call_id": block.get("tool_use_id"),
                    })
                else:
                    # A plain string is already the text; only a block list
                    # needs flattening. Passing a {"type":"text"} dict back
                    # through _text_of would read its *keys* — that bug shipped
                    # once and rewrote every prompt to "typetext".
                    text = block if isinstance(block, str) else _text_of([block])
                    out.append({"role": "user",
                                "content": [{"type": "text", "text": text}]})

        else:
            out.append({"role": role or "user",
                        "content": [{"type": "text", "text": _text_of(content)}]})
    return out


def anthropic_tools_to_trae(tools) -> list[dict]:
    """Anthropic tool defs -> Trae's OpenAI-style list.

    ``parameters`` MUST be a JSON string. An object makes Trae answer HTTP 500
    with a body-less "Internal Server Error".
    """
    out = []
    for tool in tools or []:
        name = tool.get("name")
        if not name:
            continue
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": tool.get("description") or "",
                "parameters": json.dumps(
                    tool.get("input_schema") or {"type": "object", "properties": {}}),
            },
        })
    return out


def _stop_reason(finish_reason: str, had_tool_call: bool) -> str:
    if had_tool_call:
        return "tool_use"
    if finish_reason in ("length", "max_tokens"):
        return "max_tokens"
    return "end_turn"


class TraeStream:
    """One llm_raw_chat call, run on a thread and surfaced as an event queue."""

    def __init__(self, auth: TraeAuth, model: str, messages, system, tools,
                 max_tokens=None, temperature=None, verbose=False):
        self.auth = auth
        self.verbose = verbose
        self.model = model
        self.messages = messages
        self.system = system
        self.tools = tools
        self.max_tokens = max_tokens
        self.temperature = temperature

    def resolve(self) -> dict:
        """Map a requested name onto a catalogue entry."""
        cat = self.auth.catalogue()
        if not cat:
            raise RuntimeError("the Trae subscription returned an empty model list")
        want = (self.model or "").strip()
        for entry in cat:
            if want in (entry["config_name"], entry["model_name"], entry["display_name"]):
                return entry
        lw = want.lower()
        for entry in cat:
            if lw in (entry["config_name"].lower(), entry["display_name"].lower()):
                return entry
        default = next((e for e in cat if "DeepSeek-V4.1-Flash" in e["config_name"]),
                       cat[0])
        if self.verbose:
            print("[trae] unknown model %r -> %s" % (want, default["config_name"]),
                  file=sys.stderr, flush=True)
        return default

    def start(self) -> "queue.Queue":
        q: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, args=(q,), daemon=True).start()
        return q

    def _run(self, q: queue.Queue) -> None:
        try:
            entry = self.resolve()
            session_id = str(uuid.uuid4())
            loop_id = str(uuid.uuid4())
            body = {
                "config_name": entry["config_name"],
                "conversation_id": session_id,
                "messages": anthropic_to_trae(self.messages, self.system),
                "model_name": entry["model_name"],
                "session_id": session_id,
                "tools": anthropic_tools_to_trae(self.tools),
                "user_input": "",
            }
            tok = self.auth.jwt()
            logid = TraeAuth.logid()
            extra = {
                "agent_loop_id": loop_id,
                "api_host": TRAE_HOST,
                "api_key": tok,
                "base_url": TRAE_HOST + "/trae-cli/api/v1/llm/proxy",
                "config_name": entry["config_name"],
                "config_source": 1,
                "display_name": entry["display_name"],
                "model_name": entry["model_name"],
                "real_api_key": "",
                "real_base_url": "",
                "session_id": session_id,
                "user_prompt_submit_id": loop_id,
            }
            req = urllib.request.Request(
                TRAE_HOST + "/api/ide/v2/llm_raw_chat",
                data=json.dumps(body).encode(), method="POST")
            for k, v in {
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Authorization": "Cloud-IDE-JWT " + tok,
                "Extra": json.dumps(extra),
                "X-App-Id": TRAE_APP_ID,
                "X-Client-Request-Id": logid,
                "X-Ide-Function": "chat",
                "X-Ide-Version": TRAE_IDE_VERSION,
                "X-Ide-Version-Code": TRAE_IDE_VERSION_CODE,
                "X-Tt-Logid": logid,
            }.items():
                req.add_header(k, v)

            event = None
            for raw in self.auth._opener.open(req, timeout=900):
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line.startswith("event: "):
                    event = line[7:].strip()
                elif line.startswith("data: "):
                    data = line[6:]
                    if event == "output":
                        try:
                            q.put(("output", json.loads(data)))
                        except json.JSONDecodeError:
                            pass
                    elif event in ("done", "token_usage", "metadata"):
                        q.put((event, data))
            q.put(("eof", None))
        except urllib.error.HTTPError as e:
            detail = e.read()[:500].decode("utf-8", "replace")
            q.put(("error", "Trae upstream HTTP %d: %s" % (e.code, detail)))
        except Exception as e:
            q.put(("error", "%s: %s" % (type(e).__name__, e)))


# ── HTTP server ───────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "trae-bridge"
    auth: TraeAuth = None
    verbose = False
    # Whether the current response body uses chunked framing, so _sse knows
    # whether to prefix each event with its chunk size.
    _chunked = False

    def log_message(self, fmt, *args):
        if self.verbose:
            sys.stderr.write("[trae] " + (fmt % args) + "\n")

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path in ("/health", "/health/liveliness"):
            self._json(200, {"status": "ok", "provider": "trae"})
        elif path in ("/v1/models", "/models"):
            try:
                cat = self.auth.catalogue()
            except Exception as e:
                self._json(500, {"error": {"message": str(e)}})
                return
            self._json(200, {"data": [
                {"id": e["config_name"], "object": "model", "owned_by": "trae",
                 "display_name": e["display_name"], "context_window": e["ctx"],
                 "max_output": e["max_output"], "vision": e["vision"]}
                for e in cat]})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError as e:
            self._json(400, {"type": "error", "error": {
                "type": "invalid_request_error", "message": str(e)}})
            return
        if path == "/v1/messages":
            self._messages(payload)
        elif path == "/v1/messages/count_tokens":
            # Claude Code only uses this to size the context window.
            self._json(200, {"input_tokens": max(1, len(raw) // 4)})
        else:
            self._json(404, {"error": {"message": "not found: " + path}})

    def _end_chunked(self) -> None:
        """Write the terminating zero-length chunk."""
        if self._chunked:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    def _sse(self, event: str, data: dict) -> None:
        # Emit as one HTTP chunk when chunked framing is in use, so the client
        # can parse each event as it arrives.
        body = ("event: %s\ndata: %s\n\n"
                % (event, json.dumps(data, ensure_ascii=False))).encode()
        if self._chunked:
            self.wfile.write(("%x\r\n" % len(body)).encode() + body + b"\r\n")
        else:
            self.wfile.write(body)
        self.wfile.flush()

    def _messages(self, payload: dict) -> None:
        runner = TraeStream(
            auth=self.auth,
            model=payload.get("model") or "",
            messages=payload.get("messages") or [],
            system=payload.get("system"),
            tools=payload.get("tools"),
            max_tokens=payload.get("max_tokens"),
            temperature=payload.get("temperature"),
            verbose=self.verbose,
        )
        q = runner.start()

        # Peek the first upstream event before committing to a status code, so
        # an upstream failure is still answerable as a normal HTTP error rather
        # than a half-written 200 stream.
        first = q.get()
        if first[0] == "error":
            self._json(502, {"type": "error",
                             "error": {"type": "api_error", "message": first[1]}})
            return
        if payload.get("stream"):
            self._streaming(q, first, payload.get("model") or "")
        else:
            self._non_streaming(q, first, payload.get("model") or "")

    @staticmethod
    def _drain(q, first):
        """Yield (kind, parsed) for every upstream event, exactly once.

        Both response shapes consume the same stream; keeping the walk in one
        place is deliberate — the two hand-rolled copies this replaces each had
        the same bug, where a `continue` on a non-output event skipped the next
        `q.get()` and silently truncated the turn.
        """
        item = first
        while item is not None:
            kind, data = item
            # Test for the terminator BEFORE pulling the next event: the
            # producer stops after `eof`, so fetching first would block
            # forever on an empty queue. The fetch then sits ahead of the
            # `continue`s below, so no branch can skip it.
            if kind in ("eof", "error"):
                return
            item = q.get()
            if kind == "output":
                yield kind, data
            else:
                try:
                    yield kind, json.loads(data)
                except (json.JSONDecodeError, TypeError):
                    continue

    def _streaming(self, q, first, model) -> None:
        msg_id = "msg_trae_" + uuid.uuid4().hex[:24]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # Chunked framing is what lets the client see the end of the body.
        # `Connection: keep-alive` with neither Content-Length nor chunked
        # encoding leaves a strict HTTP/1.1 client (Node's fetch, i.e. Claude
        # Code) waiting for an EOF that never comes: the events arrive but the
        # turn hangs until timeout.
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self._chunked = True

        next_index = 0
        text_index: int | None = None
        tools: dict[int, dict] = {}
        usage = {"input_tokens": 0, "output_tokens": 0}
        finish = ""

        def open_text():
            nonlocal next_index, text_index
            if text_index is None:
                text_index = next_index
                next_index += 1
                self._sse("content_block_start", {
                    "type": "content_block_start", "index": text_index,
                    "content_block": {"type": "text", "text": ""}})

        def close_text():
            nonlocal text_index
            if text_index is not None:
                self._sse("content_block_stop", {"type": "content_block_stop",
                                                 "index": text_index})
                text_index = None

        try:
            self._sse("message_start", {"type": "message_start", "message": {
                "id": msg_id, "type": "message", "role": "assistant",
                "model": model, "content": [], "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0}}})

            for kind, data in self._drain(q, first):
                if kind == "token_usage":
                    usage["input_tokens"] = (data.get("prompt_tokens")
                                             or usage["input_tokens"])
                    usage["output_tokens"] = (data.get("completion_tokens")
                                              or usage["output_tokens"])
                    continue
                if kind == "done":
                    finish = data.get("finish_reason") or ""
                    continue
                if kind != "output":
                    continue

                if data.get("response"):
                    open_text()
                    self._sse("content_block_delta", {
                        "type": "content_block_delta", "index": text_index,
                        "delta": {"type": "text_delta", "text": data["response"]}})

                for call in data.get("tool_calls") or []:
                    idx = call.get("index", 0)
                    fn = call.get("function_call") or {}
                    if idx not in tools:
                        close_text()
                        slot = {
                            "index": next_index,
                            "id": call.get("id") or "toolu_trae_%d_%s"
                                  % (idx, uuid.uuid4().hex[:12]),
                            "name": fn.get("name") or "",
                        }
                        next_index += 1
                        tools[idx] = slot
                        self._sse("content_block_start", {
                            "type": "content_block_start", "index": slot["index"],
                            "content_block": {"type": "tool_use", "id": slot["id"],
                                              "name": slot["name"] or "tool",
                                              "input": {}}})
                    if fn.get("arguments"):
                        self._sse("content_block_delta", {
                            "type": "content_block_delta",
                            "index": tools[idx]["index"],
                            "delta": {"type": "input_json_delta",
                                      "partial_json": fn["arguments"]}})

            close_text()
            for idx in sorted(tools):
                self._sse("content_block_stop", {"type": "content_block_stop",
                                                 "index": tools[idx]["index"]})
            self._sse("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": _stop_reason(finish, bool(tools)),
                          "stop_sequence": None},
                "usage": usage})
            self._sse("message_stop", {"type": "message_stop"})
            self._end_chunked()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self._chunked = False

    def _non_streaming(self, q, first, model) -> None:
        text = ""
        calls: dict[int, dict] = {}
        usage = {"input_tokens": 0, "output_tokens": 0}
        finish = ""
        for kind, data in self._drain(q, first):
            if kind == "token_usage":
                usage["input_tokens"] = (data.get("prompt_tokens")
                                         or usage["input_tokens"])
                usage["output_tokens"] = (data.get("completion_tokens")
                                          or usage["output_tokens"])
            elif kind == "done":
                finish = data.get("finish_reason") or ""
            elif kind == "output":
                text += data.get("response") or ""
                for call in data.get("tool_calls") or []:
                    idx = call.get("index", 0)
                    fn = call.get("function_call") or {}
                    slot = calls.setdefault(idx, {
                        "id": call.get("id") or "toolu_trae_%d" % idx,
                        "name": "", "args": ""})
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    slot["args"] += fn.get("arguments") or ""

        blocks = []
        if text:
            blocks.append({"type": "text", "text": text})
        for idx in sorted(calls):
            slot = calls[idx]
            try:
                parsed = json.loads(slot["args"] or "{}")
            except json.JSONDecodeError:
                parsed = {}
            blocks.append({"type": "tool_use", "id": slot["id"],
                           "name": slot["name"] or "tool", "input": parsed})
        if not blocks:
            blocks.append({"type": "text", "text": ""})

        self._json(200, {
            "id": "msg_trae_" + uuid.uuid4().hex[:24],
            "type": "message", "role": "assistant", "model": model,
            "content": blocks,
            "stop_reason": _stop_reason(finish, bool(calls)),
            "stop_sequence": None, "usage": usage})


def _load_secrets() -> None:
    """Adopt TRAECLI_PERSONAL_ACCESS_TOKEN from ccp's own secrets files."""
    for path in (Path.home() / ".config" / "litellm" / "proxy.env",
                 Path.home() / ".ccp" / "secrets.env",
                 Path.home() / ".env"):
        if not path.is_file():
            continue
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if line.startswith("TRAECLI_PERSONAL_ACCESS_TOKEN=") and "=" in line:
                    os.environ.setdefault(
                        "TRAECLI_PERSONAL_ACCESS_TOKEN",
                        line.split("=", 1)[1].strip().strip('"').strip("'"))
        except OSError:
            pass


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--serve", action="store_true", help="run the Anthropic endpoint")
    ap.add_argument("--check", action="store_true", help="validate auth, print identity")
    ap.add_argument("--list-models", action="store_true")
    ap.add_argument("--list-models-json", action="store_true",
                    help="catalogue as JSON on stdout (for ccp discover)")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--token", help="override TRAECLI_PERSONAL_ACCESS_TOKEN")
    args = ap.parse_args()

    _load_secrets()
    auth = TraeAuth(pat=args.token, verbose=args.verbose)
    auth.jwt()

    if args.list_models_json:
        # Machine-readable catalogue; nothing else may pollute stdout.
        print(json.dumps(auth.catalogue()))
        return

    if args.check or args.list_models:
        data = auth.identity.get("Data") or auth.identity.get("data") or {}
        who = (data.get("email") or data.get("Name") or data.get("name")
               or "(identity unavailable)")
        print("Trae subscription: %s" % who)
        cat = auth.catalogue()
        print("Models available: %d" % len(cat))
        if args.list_models:
            for e in cat:
                print("  %-24s %-24s ctx=%-7d out=%-6d %s"
                      % (e["config_name"], e["display_name"], e["ctx"],
                         e["max_output"], "vision" if e["vision"] else ""))
        if not args.serve:
            return

    if not args.serve:
        ap.print_help()
        return

    Handler.auth = auth
    Handler.verbose = args.verbose
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print("trae bridge: Anthropic endpoint on http://%s:%d"
          % (args.host, args.port), file=sys.stderr, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
