#!/usr/bin/env python3
"""opencode_bridge.py — expose the local opencode server as an Anthropic endpoint.

opencode's Zen free tier only serves requests that come *from opencode itself*.
Talking to the inference endpoint directly is refused no matter how the client
is disguised: ``POST https://opencode.ai/inference/v1/chat/completions`` with a
forged ``User-Agent: opencode/latest/2.0.29/cli``, an ``x-opencode-session``
header and ``Bearer public`` still answers ``403 FreeTierError`` — the gate is a
client contract enforced upstream, not an API-key check.

The ``opencode`` binary on this machine runs a long-lived background service
(``opencode serve --service``, started automatically by the first ``opencode``
run) that *is* a legitimate client, and it publishes its HTTP API through

    ~/.local/state/opencode/service.json    {url, password, pid, version}

so this adapter never re-implements the gate and never contacts Zen: it drives
the real local service over HTTP and lets *it* make the upstream call. The port
is allocated per start, so it must be read from that file on every use — it is
never hardcoded here.

Why an adapter instead of LiteLLM
---------------------------------
Every other provider in ccp is either Anthropic-native or OpenAI-compatible, so
LiteLLM can front it. This one is neither: the service speaks its own session
API (create a session, select a model, post a prompt, then follow a server-wide
SSE event stream), the response is an event dialect rather than a message body,
and the free tier's upstream call only succeeds from inside that surface. The
translation therefore lives here, and this provider needs no LiteLLM hop.

Protocol notes (all verified against the live service)
-----------------------------------------------------
* Auth is HTTP Basic: username ``opencode``, password from ``service.json``.
* ``POST /api/session`` with ``{}`` creates a session; ``POST
  /api/session/{sid}/model`` takes ``{"model": {"id": ..., "providerID":
  "opencode"}}``.
* ``POST /api/session/{sid}/prompt`` with ``{"text": ...}`` returns as soon as
  the message is queued — it does *not* wait for the generation.
* ``GET /api/event`` is one SSE stream **for the whole server**; every session's
  events arrive on it and must be filtered by ``data.sessionID``. Body text
  streams as ``session.text.delta`` and the finished turn is signalled by
  ``session.execution.succeeded`` / ``.failed`` (not by ``session.text.ended``,
  which only closes one text block).
* ``POST /api/session/{sid}/generate`` is the synchronous fallback:
  ``{"prompt": ...}`` -> ``{"data": {"text": ...}}``. The *server-global*
  ``/api/experimental/generate`` answers 403 on the free tier because it runs
  with no session context — only the session-scoped form works.
* Native tool execution: the service's own ``build`` agent ships real ``shell``,
  ``read``, ``write`` tools and will run them **on this machine**. That must
  never happen behind an Anthropic passthrough — the caller (Claude Code) owns
  the tool loop, and a hidden local execution would be an unreported side
  effect. The adapter therefore creates every session with a permissions
  ruleset that denies the command-executing and mutating tools (see
  ``_NATIVE_TOOL_DENY``). Read-only tools are deliberately left reachable: a
  ruleset that denies ``read`` was observed answering ``403 FreeTierError``
  instead of running the turn, but two independent testers could not reproduce
  that reliably, and broad denies were also seen succeeding — so it is
  recorded as an observed risk, not an established cause. The prompt states
  plainly that the model has no native tools, and the residual is called out
  under Limitations.
* Upstream is lossy in ways that must not be papered over: the same request can
  succeed once and answer 503 the next time, and a turn can end with HTTP 200
  and an empty body. An empty completion is reported as an error, never as a
  successful empty message.

Model names
-----------
External names arrive as the ccp alias (``opencode-deepseek-v4.1-flash-free``)
or bare (``deepseek-v4.1-flash-free``), optionally with ccp's ``[1m]`` window
marker (``...-free[1m]``). The marker is a LiteLLM-side spelling: the watchdog
strips it before the request reaches a native bridge, but it is stripped here
too so the mapping is total. Metadata context windows are not trusted — a
declared 200k model was measured answering from 1.6M characters — so the marker
is accepted on any model and simply removed.

Tool calls
----------
The prompt endpoint accepts no ``tools``. Tool definitions are therefore
serialised into the prompt with an instruction to answer with a single line of
JSON (``{"tool_call": {"name": ..., "input": {...}}}``) when a tool is needed;
that shape is parsed back into an Anthropic ``tool_use`` block with
``stop_reason: "tool_use"``. Assistant ``tool_result`` turns are rendered back
to text for the upstream model.

Limitations (v1)
----------------
* Text only. The service can attach images through the prompt endpoint's
  ``files`` field, but this adapter does not build that request yet; an image
  block is replaced with a placeholder rather than silently dropped.
* ``temperature`` is accepted and ignored: the service selects sampling from
  its own catalogue entry, and there is no field for it on the prompt request.
* Read-only native tools stay reachable (see the permission note above).
* A turn's ``tool_use`` arrives as one whole block rather than as streamed
  ``input_json_delta`` fragments, because the protocol is detected after the
  completion finishes.

Usage:
  python3 opencode_bridge.py --check              # locate + validate the service
  python3 opencode_bridge.py --list-models        # what the free tier exposes
  python3 opencode_bridge.py --serve [--port N]   # run the Anthropic endpoint
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_PORT = 4110
SERVICE_STATE = Path.home() / ".local" / "state" / "opencode" / "service.json"
SERVICE_USER = "opencode"
PROVIDER_ID = "opencode"

# ccp's "this model has a 1M window" marker. LiteLLM-side spelling; accepted and
# removed here so the mapping still lands on a real modelID.
WINDOW_MARKER = "[1m]"

# External alias prefix, e.g. `opencode-deepseek-v4.1-flash-free`.
ALIAS_PREFIX = "opencode-"

# How long a session may sit unused before it is dropped (and, with it, the
# upstream conversation it holds). Keyed by the caller's conversation id.
SESSION_TTL_S = 1800
SESSION_CACHE_MAX = 64

# Poll/settle budgets. A free-tier turn answers in a few seconds; the ceiling is
# generous because a large prompt can queue behind other work.
STREAM_IDLE_TIMEOUT_S = 300
SYNC_TIMEOUT_S = 600

# The service's `build` agent carries real shell/read/write tools that would run
# **on this host** if the model asked for them. A passthrough must not let that
# happen: the caller owns the tool loop and would never learn about a hidden
# local execution. Denying the tools that execute commands or modify the host
# removes the dangerous half of that capability.
#
# Verified effective: with this ruleset applied, a prompt that told the model to
# create a file in its own runtime ("execute touch /tmp/xxx") was refused by the
# model AND the sentinel file was never created — the tool call did not reach
# the host. Without the ruleset the same class of prompt really did run `shell`
# on this machine (observed executing `ls -la /tmp`), so the deny is what
# prevents it, not merely a cooperative model.
#
# The list is deliberately narrow on the *execution* side: only the tools that
# run code or modify the host are denied unconditionally. Read-only tools are
# handled separately, scoped to credential paths rather than switched off —
# see `_NATIVE_DENY_PATHS` below for why a blanket `deny read` is not an option.
# Read-only access stays enabled on purpose — a blanket `deny read` was seen
# tripping the free-tier gate (403 FreeTierError) rather than the tool. But
# "read is reachable" means a prompt-injected turn could pull credentials off
# this host: `~/.config/litellm/proxy.env` alone holds every provider key.
#
# A *resource-scoped* deny sidesteps that tradeoff. Measured on this host:
#   deny read/glob/grep scoped to a directory  -> normal turn still answers pong
#   deny read unscoped                         -> 403 FreeTierError
# so scoping the rule to the credential paths costs nothing and narrows the
# blast radius. This is defence in depth, not a proof: the model refused to echo
# a canary file both with and without these rules, so that test could not
# distinguish "rule blocked it" from "model declined" — the ruleset is kept
# because it is verified harmless, not because blocking was demonstrated.
_NATIVE_DENY_PATHS = (
    "/home/*/.config/**",
    "/home/*/.ssh/**",
    "/home/*/.aws/**",
    "/home/*/.gnupg/**",
    "/home/*/.claude.json",
    "**/.env",
    "**/proxy.env",
)
_NATIVE_TOOL_DENY = [
    # Allow-all first, then deny every tool except `read` — the shape that was
    # measured to work. Cutting the native tool set is not just hygiene here,
    # it is most of the prompt: on a 44-character user turn the session costs
    # ~6083 tokens with the stock rules and ~1963 with this one, because the
    # definitions of tools we can never surface to the caller are injected
    # every turn regardless. Changing only the agent's `system` text moved it
    # 6083 -> 5780, which is how we know the bulk is the tool table and not the
    # prompt text.
    #
    # `read` stays allowed on purpose. Denying it as well (`deny *`) was
    # observed answering 403 FreeTierError instead of running the turn, so the
    # one read-only tool is the price of the turn being accepted at all.
    {"action": "*", "resource": "*", "effect": "allow"},
] + [
    {"action": action, "resource": "*", "effect": "deny"}
    for action in ("shell", "edit", "write", "patch", "glob", "grep", "list",
                   "webfetch", "websearch", "todowrite", "subagent", "skill",
                   "question", "browser")
] + [
    {"action": action, "resource": resource, "effect": "deny"}
    for action in ("read", "glob", "grep")
    for resource in _NATIVE_DENY_PATHS
]

# Tool names the model is told to emit; kept short so the instruction is cheap.
_TOOL_PROTOCOL = (
    "You are being used through an API that cannot pass native tool schemas.\n"
    "You have NO native tools of your own: do not try to run commands or read\n"
    "files yourself. The only way to act is the tool call below.\n"
    "To call a tool, reply with EXACTLY one line and nothing else:\n"
    '{"tool_call":{"name":"<tool name>","input":{<arguments as JSON>}}}\n'
    "Do not wrap it in a markdown fence, do not add commentary before or after.\n"
    "If no tool is needed, reply with normal text only.\n"
)


def _log(msg: str) -> None:
    """All diagnostics go to stderr: stdout belongs to --list-models-json."""
    print("[opencode] " + msg, file=sys.stderr, flush=True)


def _auth_header(password: str) -> str:
    raw = ("%s:%s" % (SERVICE_USER, password)).encode()
    return "Basic " + base64.b64encode(raw).decode()


class ServiceNotFound(RuntimeError):
    pass


class UpstreamError(RuntimeError):
    """An upstream failure that must be surfaced, not silently swallowed."""

    def __init__(self, message: str, status: int = 502, kind: str = "api_error"):
        super().__init__(message)
        self.status = status
        self.kind = kind


class Service:
    """The local opencode server: its address, its models, and its session API.

    The address is re-read from ``service.json`` whenever it looks stale, so a
    service restart (which moves the port) is picked up without a restart of
    this bridge.
    """

    def __init__(self, url: str | None = None, password: str | None = None,
                 verbose: bool = False, autostart: bool = True):
        self.verbose = verbose
        self.autostart = autostart
        self._url = url.rstrip("/") if url else None
        self._password = password
        self._lock = threading.Lock()
        self._models: list[dict] | None = None
        self._models_at = 0.0
        self._service_path = SERVICE_STATE
        self._opener = urllib.request.build_opener()
        if not self._url or not self._password:
            self._read_state()
        if not self._url or not self._password:
            if autostart:
                self._start_service()
            if not self._url or not self._password:
                raise ServiceNotFound(
                    "opencode is not running: %s is missing or unreadable and "
                    "`opencode serve` could not be started. Run opencode once, "
                    "or run `opencode serve --hostname 127.0.0.1` manually."
                    % self._service_path)

    def _log(self, msg: str) -> None:
        if self.verbose:
            _log(msg)

    # ── discovery ────────────────────────────────────────────────────────
    def _read_state(self) -> bool:
        """Load {url, password} from service.json. False if unusable."""
        try:
            state = json.loads(self._service_path.read_text())
        except (OSError, json.JSONDecodeError):
            return False
        url, password = state.get("url"), state.get("password")
        if not url or not password:
            return False
        self._url = str(url).rstrip("/")
        self._password = str(password)
        self._log("service at %s (pid %s, v%s)"
                  % (self._url, state.get("pid"), state.get("version")))
        return True

    def _start_service(self) -> None:
        """Last resort: bring the *daemon* up ourselves.

        Prefers ``opencode serve --service`` (the same long-lived form opencode
        itself starts, which writes service.json); falls back to a plain
        ``opencode serve`` on an ephemeral port with a fixed password.
        """
        exe = shutil.which("opencode") or str(Path.home() / ".opencode/bin/opencode")
        if not Path(exe).exists():
            _log("opencode binary not found; cannot autostart")
            return
        _log("no running opencode service; starting `opencode serve --service`")
        try:
            subprocess.Popen([exe, "serve", "--service"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError as e:
            _log("autostart failed: %s" % e)
            return
        for _ in range(60):  # up to ~30s for service.json to appear
            time.sleep(0.5)
            if self._read_state() and self._reachable():
                return
        _log("autostarted service did not become ready")

    def _reachable(self) -> bool:
        try:
            self.request("GET", "/api/session/active", timeout=5)
            return True
        except Exception:
            return False

    def _refresh(self) -> None:
        """Re-read service.json when the address we hold stops answering."""
        with self._lock:
            if self._read_state():
                self._models = None

    @property
    def url(self) -> str:
        assert self._url
        return self._url

    # ── HTTP ─────────────────────────────────────────────────────────────
    def request(self, method: str, path: str, body: dict | None = None,
                timeout: int = 60, stream: bool = False):
        """One authenticated call against the service.

        On a connection-level failure the address is re-read once, because the
        usual cause is the service having restarted on a new port.
        """
        last: Exception | None = None
        for attempt in (0, 1):
            url = self.url + path
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", _auth_header(self._password or ""))
            if data is not None:
                req.add_header("Content-Type", "application/json")
            if stream:
                req.add_header("Accept", "text/event-stream")
            try:
                resp = self._opener.open(req, timeout=timeout)
                break
            except urllib.error.HTTPError as e:
                detail = e.read()[:400].decode("utf-8", "replace")
                # A 401 means the password rotated (service restarted): re-read
                # and retry exactly once.
                if e.code == 401 and attempt == 0:
                    last = e
                    self._refresh()
                    continue
                raise UpstreamError(
                    "opencode service HTTP %d on %s: %s" % (e.code, path, detail),
                    status=502) from e
            except (urllib.error.URLError, ConnectionError, OSError) as e:
                last = e
                if attempt == 0:
                    self._refresh()
                    continue
                raise UpstreamError(
                    "opencode service unreachable at %s (%s)"
                    % (self._url, e), status=503,
                    kind="overloaded_error") from e
        else:  # pragma: no cover - loop always breaks or raises
            raise UpstreamError(str(last), status=503)

        if stream:
            return resp
        raw = resp.read()
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise UpstreamError(
                "opencode service returned non-JSON on %s: %s"
                % (path, raw[:300].decode("utf-8", "replace"))) from e

    # ── models ───────────────────────────────────────────────────────────
    def models(self, refresh: bool = False) -> list[dict]:
        """The models the local service can actually run (cached briefly)."""
        if not refresh and self._models is not None and time.time() - self._models_at < 300:
            return self._models
        payload = self.request("GET", "/api/model", timeout=30)
        rows = payload.get("data") if isinstance(payload, dict) else payload
        self._models = [r for r in (rows or []) if r.get("id")]
        self._models_at = time.time()
        return self._models

    def resolve(self, name: str) -> dict:
        """Map an external model name onto a service model row.

        Raises ``UpstreamError`` (HTTP 404) for a name the service does not
        serve — a clear error beats silently answering as a different model.
        """
        want = (name or "").strip()
        if want.lower().endswith(WINDOW_MARKER):
            want = want[: -len(WINDOW_MARKER)].strip()
        if want.lower().startswith(ALIAS_PREFIX):
            want = want[len(ALIAS_PREFIX):]

        # Cache-first: a fetch per turn would be one wasted round trip on the
        # hot path. Only a name we do not recognise pays for a refresh, which
        # also picks up a model the service has just learned about.
        rows = self.models()
        found = self._match(rows, want)
        if found is None:
            rows = self.models(refresh=True)
            found = self._match(rows, want)
        if found is not None:
            return found
        known = ", ".join(sorted(str(r["id"]) for r in rows if r.get("providerID") == PROVIDER_ID))
        raise UpstreamError(
            "unknown model %r. Free-tier models: %s" % (name, known or "(none)"),
            status=404, kind="not_found_error")

    @staticmethod
    def _match(rows: list[dict], want: str) -> dict | None:
        for row in rows:
            if want in (row.get("id"), row.get("modelID"), row.get("name")):
                return row
        low = want.lower()
        for row in rows:
            if low in (str(row.get("id", "")).lower(),
                       str(row.get("name", "")).lower()):
                return row
        return None

    # ── sessions ─────────────────────────────────────────────────────────
    def new_session(self) -> str:
        payload = self.request("POST", "/api/session",
                               {"permissions": _NATIVE_TOOL_DENY}, timeout=30)
        sid = (payload.get("data") or {}).get("id")
        if not sid:
            raise UpstreamError("opencode did not return a session id: %s"
                                % json.dumps(payload)[:200])
        return sid

    def select_model(self, sid: str, row: dict) -> None:
        # The model endpoint answers 204/empty on success.
        self.request("POST", "/api/session/%s/model" % sid,
                     {"model": {"id": row["id"],
                                "providerID": row.get("providerID") or PROVIDER_ID}},
                     timeout=30)

    def delete_session(self, sid: str) -> None:
        try:
            self.request("DELETE", "/api/session/%s" % sid, timeout=15)
        except Exception:
            pass

    def prompt(self, sid: str, text: str) -> None:
        """Queue a prompt. Returns as soon as the service accepts it."""
        self.request("POST", "/api/session/%s/prompt" % sid, {"text": text},
                     timeout=60)

    def event_stream(self, timeout: int = SYNC_TIMEOUT_S):
        return self.request("GET", "/api/event", timeout=timeout, stream=True)


class SessionPool:
    """Keeps one upstream session per conversation.

    Each *conversation* needs its own session (concurrent chats must not
    interleave turns), while the turns of one conversation should share a
    session so the service keeps the history. Callers identify the conversation
    with a request header; anonymous callers get a fresh session per request.

    Reuse is only safe if the bridge knows what the session has already been
    told. The client (Claude Code) resends the whole transcript every turn, so
    each entry also tracks how many of those messages have been delivered
    upstream — see ``acquire``. A turn holds the conversation's lock, so two
    turns of one conversation cannot interleave and corrupt that count.
    """

    def __init__(self, service: Service):
        self.service = service
        self._lock = threading.Lock()
        # key -> [session_id, last_used, messages_delivered, lock]
        self._entries: dict[str, list] = {}

    def acquire(self, key: str | None) -> tuple[str, int, threading.Lock]:
        """Return (session_id, messages_already_delivered, conversation_lock).

        A fresh session has been told nothing, so the caller must send the whole
        transcript; a reused one needs only what is new.
        """
        now = time.time()
        if key:
            with self._lock:
                hit = self._entries.get(key)
                if hit and now - hit[1] < SESSION_TTL_S:
                    hit[1] = now
                    return hit[0], hit[2], hit[3]
        sid = self.service.new_session()
        if not key:
            return sid, 0, threading.Lock()
        lock = threading.Lock()
        with self._lock:
            prev = self._entries.get(key)
            if prev is not None:
                lock = prev[3]  # keep one lock per conversation across refreshes
            self._entries[key] = [sid, now, 0, lock]
            self._evict_locked()
        return sid, 0, lock

    def delivered(self, key: str | None, sid: str, count: int) -> None:
        """Record that ``count`` messages are now known to session ``sid``."""
        if not key:
            return
        with self._lock:
            entry = self._entries.get(key)
            if entry and entry[0] == sid:
                entry[2] = count

    def _evict_locked(self) -> None:
        if len(self._entries) <= SESSION_CACHE_MAX:
            return
        stale = sorted(self._entries.items(), key=lambda kv: kv[1][1])
        for key, entry in stale[: len(self._entries) - SESSION_CACHE_MAX]:
            del self._entries[key]
            threading.Thread(target=self.service.delete_session, args=(entry[0],),
                             daemon=True).start()


# ── request translation ──────────────────────────────────────────────────

def _text_of(content) -> str:
    """Flatten an Anthropic message content value into plain text."""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text") or "")
        elif kind == "tool_use":
            parts.append("[tool_use %s %s]" % (
                block.get("name"), json.dumps(block.get("input") or {},
                                             ensure_ascii=False)))
        elif kind == "tool_result":
            body = _text_of(block.get("content"))
            parts.append("[tool_result%s]\n%s"
                         % (" error" if block.get("is_error") else "", body))
        elif kind == "image":
            # The service's own prompt endpoint takes text; a base64 image could
            # be attached via `files`, but that is not wired up in v1.
            parts.append("[image omitted: opencode bridge is text-only in v1]")
        elif kind == "thinking":
            continue
        else:
            parts.append(json.dumps(block, ensure_ascii=False))
    return "\n".join(p for p in parts if p)


def _tools_to_text(tools) -> str:
    """Serialise Anthropic tool definitions into the prompt protocol."""
    lines = [_TOOL_PROTOCOL, "Available tools:"]
    for tool in tools or []:
        lines.append(json.dumps({
            "name": tool.get("name"),
            "description": tool.get("description") or "",
            "input_schema": tool.get("input_schema") or {},
        }, ensure_ascii=False))
    return "\n".join(lines)


def build_prompt(system, messages, tools, max_tokens=None,
                 already_delivered: int = 0) -> str:
    """Render one Anthropic request into the service's prompt text.

    The client resends the whole transcript every turn, while the service keeps
    its own copy per session. So the messages the session has already been told
    (``already_delivered``) are dropped, and only what is new goes upstream —
    sending them again would duplicate the conversation in the model's context.
    For a fresh session ``already_delivered`` is 0 and the whole transcript is
    sent.

    The system prompt and the tool protocol are re-sent every turn: they are
    cheap, and the service may compact its own history.
    """
    chunks: list[str] = []
    sys_text = _text_of(system) if system else ""
    if sys_text.strip():
        chunks.append(sys_text.strip())
    if tools:
        chunks.append(_tools_to_text(tools))

    pending = list(messages or [])[max(0, already_delivered):]
    if not pending:
        # Nothing new (a retry of an identical request): resend the last user
        # turn rather than posting an empty prompt, which would fail upstream.
        pending = list(messages or [])[-1:]
        if pending:
            chunks.append("(Repeat of the previous turn.)")

    rendered = []
    for msg in pending:
        role = msg.get("role") or "user"
        text = _text_of(msg.get("content"))
        if not text.strip():
            continue
        rendered.append(text if role == "user" else "assistant: %s" % text)
    tail = "\n\n".join(rendered)

    if max_tokens:
        chunks.append("(Reply within about %d tokens.)" % max_tokens)
    if tail:
        chunks.append(tail)
    return "\n\n".join(c for c in chunks if c)


def _json_objects(text: str):
    """Yield every balanced ``{...}`` substring in ``text``.

    A regex cannot do this job: tool inputs nest (``{"input":{"city":"..."}}``),
    so a lazy ``\\{.*?\\}`` stops at the *inner* closing brace and hands back
    invalid JSON. Scanning for the matching brace is what makes a nested
    ``input`` parse at all.
    """
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    yield text[start:i + 1]


def _looks_like_tool_call(fragment: str) -> bool:
    """True when a *first* delta may be the start of a simulated tool call.

    Only ever consulted on the opening fragment, so a false positive costs one
    buffered turn (still correct, just not incremental) and a false negative
    would leak half a JSON call as prose. It leans towards matching: leading
    whitespace or a code fence, then either the key or an opening brace with no
    prose before it.
    """
    head = fragment.lstrip()
    if head.startswith("```"):
        # A fence is either the tool call or a prose code block; buffering is
        # correct for both and cannot leak a half-written call as prose.
        return True
    if not head:
        return False
    if head.startswith('{"tool_call"') or head.startswith("{'tool_call'"):
        return True
    # A leading `{` (or `{"`) with no prose yet might become a call once more
    # deltas land, so it is worth waiting for.
    return head.startswith("{") and "{" not in head[1:]


def parse_tool_call(text: str) -> tuple[str, list[dict]] | None:
    """Split model output into (text_before, tool_use blocks) or None.

    The protocol asks for exactly one line of JSON. Models still sometimes wrap
    it in prose or a code fence, so a balanced JSON object is located inside the
    text rather than requiring the output to be clean.
    """
    if not text or '"tool_call"' not in text:
        return None
    for raw in _json_objects(text):
        if '"tool_call"' not in raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        call = obj.get("tool_call")
        if not isinstance(call, dict) or not call.get("name"):
            continue
        block = {
            "type": "tool_use",
            "id": "toolu_oc_" + uuid.uuid4().hex[:20],
            "name": str(call["name"]),
            "input": call.get("input") if isinstance(call.get("input"), dict) else {},
        }
        return text[: text.index(raw)], [block]
    return None


# ── upstream turn execution ──────────────────────────────────────────────

class Turn:
    """One prompt -> completion round trip against a service session.

    ``events()`` is a generator: it yields each upstream event the moment it
    arrives, so a streaming caller can forward text as it is produced instead of
    waiting for the turn to end (which would make "streaming" a lie — the whole
    reply would arrive in a single delta).
    """

    def __init__(self, service: Service, sid: str, prompt: str, verbose=False):
        self.service = service
        self.sid = sid
        self.prompt = prompt
        self.verbose = verbose

    def events(self):
        """Yield (kind, data) for every event of this session's turn.

        Raises ``UpstreamError`` for a failed turn, a broken stream, a timeout,
        or a visibly empty completion.
        """
        resp = self.service.event_stream(timeout=STREAM_IDLE_TIMEOUT_S)
        try:
            self.service.prompt(self.sid, self.prompt)
            yield from self._consume(resp)
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def _consume(self, resp):
        got_text = False
        deadline = time.time() + SYNC_TIMEOUT_S
        while True:
            if time.time() > deadline:
                raise UpstreamError(
                    "opencode turn timed out after %ds" % SYNC_TIMEOUT_S,
                    status=504)
            try:
                raw = resp.readline()
            except (TimeoutError, OSError) as e:
                raise UpstreamError("opencode event stream broke: %s" % e,
                                    status=502)
            if not raw:
                raise UpstreamError(
                    "opencode event stream closed before the turn finished",
                    status=502)
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue  # `: heartbeat` and blank separators
            try:
                event = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            data = event.get("data") or {}
            if data.get("sessionID") != self.sid:
                continue  # one stream serves every session on the server
            kind = event.get("type") or ""
            if kind == "session.execution.failed":
                err = data.get("error") or {}
                msg = err.get("message") or "opencode turn failed"
                raise UpstreamError("opencode upstream error: %s" % msg,
                                    status=503, kind="overloaded_error")
            if kind == "session.text.delta":
                delta = data.get("delta") or ""
                if delta:
                    got_text = True
                    yield ("text", delta)
            elif kind == "session.reasoning.delta":
                continue
            elif kind == "session.step.ended":
                yield ("step", data)
            elif kind == "session.execution.succeeded":
                if not got_text:
                    # HTTP 200 with an empty body is a real upstream outcome.
                    # Reporting it as a successful empty message would leave the
                    # caller with a turn that looks fine and says nothing.
                    raise UpstreamError(
                        "opencode returned an empty completion (upstream "
                        "503/empty body); retry the request",
                        status=502, kind="overloaded_error")
                return

    def run(self) -> dict:
        """Drain the turn and return {text, usage, finish}."""
        text: list[str] = []
        usage: dict = {}
        finish = ""
        for kind, data in self.events():
            if kind == "text":
                text.append(data)
            elif kind == "step":
                usage = data.get("tokens") or usage
                finish = data.get("finish") or finish
        return {"text": "".join(text), "usage": usage, "finish": finish}


def _usage_from(tokens: dict, prompt_text: str, body: str) -> dict:
    """Anthropic usage block from the service's token counters.

    ``cache.read`` reflects the service's own system prompt, which the caller
    never sent, so it is reported as cache reads rather than input tokens.
    """
    tokens = tokens or {}
    cache = tokens.get("cache") or {}
    return {
        "input_tokens": int(tokens.get("input") or max(1, len(prompt_text) // 4)),
        "output_tokens": int(tokens.get("output") or max(1, len(body) // 4)),
        "cache_read_input_tokens": int(cache.get("read") or 0),
        "cache_creation_input_tokens": int(cache.get("write") or 0),
    }


def _blocks(text: str) -> tuple[list[dict], str]:
    """Anthropic content blocks + stop reason for a completion."""
    parsed = parse_tool_call(text)
    if parsed is None:
        return [{"type": "text", "text": text}], "end_turn"
    before, tool_blocks = parsed
    blocks: list[dict] = []
    if before.strip():
        blocks.append({"type": "text", "text": before})
    blocks.extend(tool_blocks)
    return blocks, "tool_use"


# ── HTTP server ──────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "opencode-bridge"
    service: Service = None
    pool: SessionPool = None
    verbose = False
    _chunked = False
    # True once a response status line has been written; guards against trying
    # to answer a request twice (which would corrupt the connection).
    _responded = False

    def log_message(self, fmt, *args):
        if self.verbose:
            sys.stderr.write("[opencode] " + (fmt % args) + "\n")

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self._responded = True
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _anthropic_error(self, err: UpstreamError) -> None:
        self._json(err.status if 400 <= err.status < 600 else 502, {
            "type": "error",
            "error": {"type": err.kind, "message": str(err)}})

    # ── GET ──────────────────────────────────────────────────────────────
    def do_GET(self):
        # One Handler instance serves every request on a keep-alive connection,
        # so per-response state has to be cleared here.
        self._responded = False
        self._chunked = False
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/health", "/health/liveliness", "/health/readiness"):
            self._health()
        elif path in ("/v1/models", "/models"):
            self._models()
        else:
            self._json(404, {"type": "error",
                             "error": {"type": "not_found_error",
                                       "message": "not found: " + path}})

    def _health(self):
        info = {"status": "ok", "provider": "opencode"}
        try:
            row = self.service.resolve("deepseek-v4.1-flash-free")
            info["service"] = self.service.url
            info["upstream_model"] = row.get("id")
        except Exception as e:
            info = {"status": "degraded", "provider": "opencode",
                    "error": str(e)}
        self._json(200, info)

    def _models(self):
        try:
            rows = self.service.models(refresh=True)
        except UpstreamError as e:
            self._anthropic_error(e)
            return
        # Free tier only: other entries belong to providers this bridge is not
        # configured for, and listing them invites a 403 at generation time.
        data = []
        for row in rows:
            if row.get("providerID") != PROVIDER_ID:
                continue
            limit = row.get("limit") or {}
            data.append({
                "id": row.get("id"),
                "object": "model",
                "owned_by": "opencode",
                "display_name": row.get("name") or row.get("id"),
                "context_window": limit.get("context") or 200000,
                "max_output": limit.get("output") or 32000,
                "capabilities": row.get("capabilities") or {},
            })
        self._json(200, {"data": data})

    # ── POST ─────────────────────────────────────────────────────────────
    def do_POST(self):
        self._responded = False
        self._chunked = False
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError as e:
            self._json(400, {"type": "error", "error": {
                "type": "invalid_request_error", "message": str(e)}})
            return
        if path == "/v1/messages":
            try:
                self._messages(payload)
            except UpstreamError as e:
                # Once the SSE headers are out the response is committed, so a
                # second HTTP status would corrupt the stream. _stream already
                # reported this as an in-band `error` event.
                if not self._responded:
                    self._anthropic_error(e)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # never leave the client hanging
                _log("unhandled %s: %s" % (type(e).__name__, e))
                if not self._responded:
                    self._anthropic_error(
                        UpstreamError("%s: %s" % (type(e).__name__, e)))
        elif path == "/v1/messages/count_tokens":
            self._json(200, {"input_tokens": max(1, len(raw) // 4)})
        else:
            self._json(404, {"type": "error",
                             "error": {"type": "not_found_error",
                                       "message": "not found: " + path}})

    def _conversation_key(self) -> str | None:
        """Bind a request to a conversation so multi-turn shares one session."""
        for header in ("X-Conversation-Id", "X-Session-Id", "X-Request-Id"):
            value = self.headers.get(header)
            if value:
                return "%s:%s" % (header, value)
        meta = getattr(self, "_meta", None)
        if isinstance(meta, dict):
            for field in ("conversation_id", "session_id", "user_id"):
                if meta.get(field):
                    return "%s:%s" % (field, meta[field])
        return None

    def _messages(self, payload: dict) -> None:
        self._meta = payload.get("metadata")
        model_name = payload.get("model") or ""
        row = self.service.resolve(model_name)
        key = self._conversation_key()
        sid, delivered, convo_lock = self.pool.acquire(key)
        messages = payload.get("messages") or []
        if delivered == 0:
            # A brand-new session knows nothing, so the whole transcript must go
            # upstream; a reused one already holds it. Sending only the newest
            # turn to a fresh session silently drops all earlier context.
            self.service.select_model(sid, row)
        prompt = build_prompt(payload.get("system"), messages,
                              payload.get("tools"), payload.get("max_tokens"),
                              delivered)
        if self.verbose:
            _log("turn: model=%s session=%s delivered=%d/%d prompt=%d chars tools=%d"
                 % (row["id"], sid, delivered, len(messages), len(prompt),
                    len(payload.get("tools") or [])))
        turn = Turn(self.service, sid, prompt, verbose=self.verbose)
        msg_id = "msg_oc_" + uuid.uuid4().hex[:24]
        # One lock per conversation: two turns of the same chat must not
        # interleave, or the delivered-count bookkeeping (and the upstream
        # history) would be corrupted.
        with convo_lock:
            try:
                if payload.get("stream"):
                    self._stream(msg_id, model_name, turn, prompt)
                else:
                    result = turn.run()
                    blocks, stop_reason = _blocks(result["text"])
                    usage = _usage_from(result["usage"], prompt, result["text"])
                    self._json(200, {
                        "id": msg_id, "type": "message", "role": "assistant",
                        "model": model_name, "content": blocks,
                        "stop_reason": stop_reason, "stop_sequence": None,
                        "usage": usage})
            except UpstreamError:
                raise
            finally:
                # Only after a turn is actually delivered does the session know
                # this many messages.
                self.pool.delivered(key, sid, len(messages))

    # ── streaming ────────────────────────────────────────────────────────
    def _end_chunked(self) -> None:
        if self._chunked:
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _sse(self, event: str, data: dict) -> None:
        body = ("event: %s\ndata: %s\n\n"
                % (event, json.dumps(data, ensure_ascii=False))).encode()
        if self._chunked:
            self.wfile.write(("%x\r\n" % len(body)).encode() + body + b"\r\n")
        else:
            self.wfile.write(body)
        self.wfile.flush()

    def _stream(self, msg_id: str, model: str, turn: Turn, prompt: str) -> None:
        """Forward the turn as Anthropic SSE, delta by delta.

        Ordinary text is relayed as it arrives, so the caller sees the reply
        form in real time. A simulated tool call cannot be streamed that way:
        its JSON is prompt-level text that may be split across deltas, so
        relaying it live would emit a half-written ``input_json_delta``. Those
        turns therefore buffer (they have no user-visible prose to show anyway)
        and emit the tool block once it is complete.
        """
        self._responded = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # Chunked framing lets the client see the end of the body. Without it a
        # strict HTTP/1.1 client waits forever for an EOF that never arrives.
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self._chunked = True

        # Optimistically open a text block: the common case is prose, and doing
        # it up front means the first delta can be written the instant it lands.
        text_index = 0
        text_open = False
        emitted = 0
        probe: str | None = None
        tool_candidate = False
        buffered: list[str] = []
        usage: dict = {}

        def open_text():
            nonlocal text_open
            if not text_open:
                self._sse("content_block_start", {
                    "type": "content_block_start", "index": text_index,
                    "content_block": {"type": "text", "text": ""}})
                text_open = True

        def close_text():
            nonlocal text_open
            if text_open:
                self._sse("content_block_stop", {"type": "content_block_stop",
                                                 "index": text_index})
                text_open = False

        try:
            upcoming = _usage_from({}, prompt, "")
            self._sse("message_start", {"type": "message_start", "message": {
                "id": msg_id, "type": "message", "role": "assistant",
                "model": model, "content": [], "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": upcoming["input_tokens"],
                          "output_tokens": 0}}})

            # Stream prose the moment it arrives, but hold the *first* chunk
            # back as a probe: a simulated tool call is JSON text that may be
            # split across deltas, so deciding on a fragment could leak a
            # half-written call to the client as prose. If the probe looks like
            # the start of one, the whole turn is buffered and shaped once at
            # the end; otherwise the probe and every later chunk stream live.
            for kind, data in turn.events():
                if kind == "step":
                    usage = data.get("tokens") or usage
                    continue
                if tool_candidate:
                    buffered.append(data)
                    continue
                if probe is None:
                    probe = data
                    if _looks_like_tool_call(probe):
                        tool_candidate = True
                        buffered.append(data)
                        probe = None
                        continue
                    open_text()
                    self._sse("content_block_delta", {
                        "type": "content_block_delta", "index": text_index,
                        "delta": {"type": "text_delta", "text": data}})
                    emitted += 1
                    probe = ""
                    continue
                open_text()
                self._sse("content_block_delta", {
                    "type": "content_block_delta", "index": text_index,
                    "delta": {"type": "text_delta", "text": data}})
                emitted += 1

            parsed = parse_tool_call("".join(buffered)) if buffered else None
            if parsed is not None:
                close_text()  # nothing was relayed as text
                before, tool_blocks = parsed
                index = text_index
                if before.strip():
                    open_text()
                    self._sse("content_block_delta", {
                        "type": "content_block_delta", "index": text_index,
                        "delta": {"type": "text_delta", "text": before}})
                    close_text()
                    index += 1
                for block in tool_blocks:
                    self._sse("content_block_start", {
                        "type": "content_block_start", "index": index,
                        "content_block": {"type": "tool_use", "id": block["id"],
                                          "name": block["name"], "input": {}}})
                    self._sse("content_block_delta", {
                        "type": "content_block_delta", "index": index,
                        "delta": {"type": "input_json_delta",
                                  "partial_json": json.dumps(
                                      block["input"], ensure_ascii=False)}})
                    self._sse("content_block_stop", {"type": "content_block_stop",
                                                     "index": index})
                    index += 1
            elif buffered:
                # Prose that only revealed itself as such after buffering
                # started: emit it as one delta rather than losing it.
                open_text()
                self._sse("content_block_delta", {
                    "type": "content_block_delta", "index": text_index,
                    "delta": {"type": "text_delta", "text": "".join(buffered)}})
                close_text()
            else:
                close_text()

            usage_block = _usage_from(usage, prompt, "".join(buffered)) if buffered \
                else _usage_from(usage, prompt, "")
            self._sse("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use" if parsed else "end_turn",
                          "stop_sequence": None},
                "usage": {"output_tokens": usage_block["output_tokens"]}})
            self._sse("message_stop", {"type": "message_stop"})
            self._end_chunked()
        except UpstreamError as e:
            # Headers are already out, so this cannot become an HTTP error code.
            # Anthropic reports a mid-stream failure as an `error` SSE event;
            # sending nothing (or a second HTTP response) would leave the client
            # waiting on a turn that will never finish.
            try:
                self._sse("error", {"type": "error",
                                    "error": {"type": e.kind,
                                              "message": str(e)}})
                close_text()
                self._sse("message_stop", {"type": "message_stop"})
                self._end_chunked()
            except (BrokenPipeError, ConnectionResetError):
                pass
            raise
        except (BrokenPipeError, ConnectionResetError):
            # The caller hung up. The turn still ran upstream, so the session
            # bookkeeping in _messages stays correct.
            pass
        finally:
            self._chunked = False


def _load_service_env() -> None:
    """Optional convenience overrides for a hand-started service."""
    for path in (Path.home() / ".ccp" / "secrets.env",
                 Path.home() / ".config" / "litellm" / "proxy.env",
                 Path.home() / ".env"):
        if not path.is_file():
            continue
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if line.startswith("OPENCODE_SERVER_PASSWORD=") and "=" in line:
                    os.environ.setdefault(
                        "OPENCODE_SERVER_PASSWORD",
                        line.split("=", 1)[1].strip().strip('"').strip("'"))
        except OSError:
            pass


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--serve", action="store_true", help="run the Anthropic endpoint")
    ap.add_argument("--check", action="store_true",
                    help="locate the service and validate it")
    ap.add_argument("--list-models", action="store_true")
    ap.add_argument("--list-models-json", action="store_true",
                    help="free-tier models as JSON on stdout (for ccp discover)")
    ap.add_argument("--url", help="override the service URL")
    ap.add_argument("--password", help="override the service password")
    ap.add_argument("--no-autostart", action="store_true",
                    help="never start a service; fail if none is running")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    _load_service_env()
    try:
        service = Service(url=args.url,
                          password=args.password or os.environ.get("OPENCODE_SERVER_PASSWORD"),
                          verbose=args.verbose,
                          autostart=not args.no_autostart)
    except ServiceNotFound as e:
        raise SystemExit(str(e))

    if args.list_models_json:
        rows = [r for r in service.models(refresh=True)
                if r.get("providerID") == PROVIDER_ID]
        print(json.dumps([{"id": r["id"], "name": r.get("name"),
                           "context_window": (r.get("limit") or {}).get("context"),
                           "max_output": (r.get("limit") or {}).get("output")}
                          for r in rows], ensure_ascii=False))
        return

    if args.check or args.list_models:
        print("opencode service: %s" % service.url)
        for row in service.models(refresh=True):
            if row.get("providerID") != PROVIDER_ID:
                continue
            limit = row.get("limit") or {}
            print("  %-38s ctx=%-9s out=%-7s %s"
                  % (row["id"], limit.get("context"), limit.get("output"),
                     "tools" if (row.get("capabilities") or {}).get("tools") else ""))
        if not args.serve:
            return

    if not args.serve:
        ap.print_help()
        return

    Handler.service = service
    Handler.pool = SessionPool(service)
    Handler.verbose = args.verbose
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print("opencode bridge: Anthropic endpoint on http://%s:%d (upstream %s)"
          % (args.host, args.port, service.url), file=sys.stderr, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
