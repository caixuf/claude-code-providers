#!/usr/bin/env python3
"""agy_bridge.py — expose the Antigravity CLI (`agy`) as an Anthropic endpoint.

ccp wants Antigravity's free quota as an ordinary provider, so this speaks the
Anthropic Messages API to Claude Code and drives the `agy` CLI underneath.

Which binary this runs is not a detail
--------------------------------------
The Antigravity CLI exists in **two** builds on this host: a native ELF binary
in WSL and a Windows `.exe` under the user profile. The Windows build
re-authenticates *per process* — every new process re-triggers a Google OAuth
prompt, which on this machine produced a storm of desktop popups during
development. The native build has no such behaviour.

Resolution therefore never goes through PATH: `which agy` returns the Windows
build here, and a shell alias (`agy = agy --dangerously-skip-permissions`)
exists on top of that. `_agy_binary()` pins an absolute path and refuses a
Windows executable outright, because picking the wrong one does not fail — it
opens browser windows on the user's desktop.

Why a resident process per conversation
---------------------------------------
Each `agy` invocation costs several seconds of cold start before the model is
even reached: a one-shot `agy -p` reports `duration_seconds: 2.4` while the
wall clock is 8–10s. Spawning per request would pay that on every turn. agy
accepts NDJSON on stdin with `--input-format stream-json` and runs one turn per
line, so a single long-lived process serves a whole conversation: measured
first turn ≈7s, second turn ≈1s.

That stdin is a single channel, so one process belongs to exactly one
conversation — which also isolates them from each other, the failure Claude
Code's concurrent haiku sub-calls would otherwise cause.

Safety: this is an agent, not a model endpoint
----------------------------------------------
agy carries a real tool set (`init.tools` reports 60 entries: bash, read,
write, browser, MCP…) and it **executes on this host**. Measured with a
sentinel file:

* no flags            -> `touch /tmp/agy_safety_*` really ran, file created
* `--sandbox`         -> still executed ("Run in a sandbox with terminal
                         restrictions" does not stop a plain `touch`)
* `--mode plan`       -> did NOT execute

`--mode plan` is therefore the default, and a later probe pins down what it
actually does, because "did not execute" was too coarse. Asked to write
`/tmp/agy_plan_write_test`, plan mode **did** call `write_to_file` — with the
target rewritten to its own scratch directory
(`~/.gemini/antigravity-cli/brain/<conversation>/…_plan.md`) — and then asked
for a "Proceed" confirmation before touching the requested path. `/tmp` was
never written. So the guarantee is narrower and more specific than "no tools
run": writes are confined to agy's own brain directory, and real actions wait
on a confirmation a non-interactive caller can never give. It is a behavioural
gate, not a capability removal — worth knowing exactly, because it means an
injected instruction cannot write outside that directory but *can* still cause
agy to invoke its tools and produce files inside it.

Claude Code's own tools are a different set that agy does not know about.
**The wire protocol cannot carry them.** Measured: `event=system`,
`event=tools` and `event=config` are each rejected with `warning: ignoring
unsupported stream input message event`, and a top-level `tools` field on the
user event is accepted without complaint but silently ignored — the model
reported "当前可用的工具中未找到名为 get_ocean_temperature 的工具" for a tool
that had just been sent that way. That is the whole reason tools are emulated:
agy's only input is a user message, so Claude Code's tools are serialised into
the prompt as text, and an emitted `{"tool_call":…}` line is translated back
into an Anthropic `tool_use` block. The model still sees its own 60-tool table
and may prefer it, or deny having the tool at all, so tool calling on this
provider is best-effort — while pure text reasoning is reliable.
"""
from __future__ import annotations

import argparse
import atexit
import errno
import hashlib
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# ── configuration ───────────────────────────────────────────────────────────

# Absolute, never resolved from PATH — see the module docstring.
AGY_BIN = os.environ.get("AGY_BIN", "/home/caixuf/.local/bin/agy")

DEFAULT_PORT = 4120
ALIAS_PREFIX = "agy-"
WINDOW_MARKER = "[1m]"

# `agy models`, measured 2026-10-15. Names are the values `--model` takes.
AGY_MODELS = {
    "gemini-3.8-flash-low",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-high",
    "claude-sonnet-4-6",
    "claude-opus-4-6-thinking",
    "gpt-oss-120b-medium",
}

FIRST_TURN_TIMEOUT_S = int(os.environ.get("AGY_FIRST_TURN_TIMEOUT", "180"))
TURN_TIMEOUT_S = int(os.environ.get("AGY_TURN_TIMEOUT", "300"))
# Max gap between two events. A long turn keeps streaming deltas, so this
# is "how long may agy go quiet", not a cap on how long a turn may take.
IDLE_TIMEOUT_S = int(os.environ.get("AGY_IDLE_TIMEOUT", "90"))
SESSION_IDLE_TTL_S = int(os.environ.get("AGY_SESSION_TTL", "1800"))
MAX_SESSIONS = int(os.environ.get("AGY_MAX_SESSIONS", "8"))

# ── logging ─────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    print("[agy] " + msg, file=sys.stderr, flush=True)


class UpstreamError(RuntimeError):
    def __init__(self, message: str, status: int = 502,
                 kind: str = "api_error"):
        super().__init__(message)
        self.status = status
        self.kind = kind


class BinaryMissing(RuntimeError):
    pass


# ── the binary ──────────────────────────────────────────────────────────────

def _agy_binary() -> str:
    """Return the pinned agy path, refusing the Windows build.

    The Windows build is not merely slower: it re-authenticates per process and
    raises a Google OAuth prompt on the desktop. Failing loudly here beats a
    bridge that appears to work while filling the screen with browser windows.
    """
    path = AGY_BIN
    if path.lower().endswith((".exe", ".bat", ".cmd")):
        raise BinaryMissing(
            "AGY_BIN points at a Windows executable (%s). That build "
            "re-authenticates every process and will raise OAuth prompts; use "
            "the native WSL binary instead." % path)
    if not os.path.exists(path):
        raise BinaryMissing(
            "agy not found at %s — install it or set AGY_BIN to its path." % path)
    return path


def resolve_model(name: str) -> str:
    """Map an external model name onto an agy `--model` value.

    Accepts the alias Claude Code is configured with, the bare id, and a
    trailing `[1m]` marker (ccp adds it for >=1M models).
    """
    want = (name or "").strip()
    if want.lower().endswith(WINDOW_MARKER):
        want = want[: -len(WINDOW_MARKER)].strip()
    if want.lower().startswith(ALIAS_PREFIX):
        want = want[len(ALIAS_PREFIX):]
    if want in AGY_MODELS:
        return want
    lowered = {m.lower(): m for m in AGY_MODELS}
    if want.lower() in lowered:
        return lowered[want.lower()]
    raise UpstreamError(
        "unknown model %r. Available: %s" % (name, ", ".join(sorted(AGY_MODELS))),
        status=404, kind="not_found_error")


# ── prompt rendering ────────────────────────────────────────────────────────

_TOOL_PROTOCOL = (
    "You are being used through an API that cannot pass native tool schemas.\n"
    "You have NO native tools of your own: do not run commands or read files\n"
    "yourself, and do not look for a tool registry. The only way to ask the\n"
    "caller to act is to reply with EXACTLY one line and nothing else:\n"
    '{"tool_call":{"name":"<tool name>","input":{<arguments as JSON>}}}\n'
    "Do not wrap it in a markdown fence and do not add commentary. If no tool "
    "is needed, reply with normal text only."
)


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                if part.get("type") == "text":
                    out.append(part.get("text") or "")
                elif part.get("type") == "tool_result":
                    body = part.get("content")
                    out.append(_text_of(body))
                elif part.get("type") == "tool_use":
                    out.append("[tool_use %s %s]" % (
                        part.get("name"), json.dumps(part.get("input") or {},
                                                    ensure_ascii=False)))
        return "\n".join(p for p in out if p)
    return ""


def _tools_to_text(tools) -> str:
    lines = []
    for tool in tools or []:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        schema = tool.get("input_schema") or tool.get("parameters") or {}
        lines.append(json.dumps({
            "name": tool["name"],
            "description": (tool.get("description") or "")[:400],
            "input_schema": schema,
        }, ensure_ascii=False))
    if not lines:
        return ""
    return ("Tools the caller can run for you (you cannot run them yourself):\n"
            + "\n".join(lines))


def build_prompt(system, messages, tools, already_delivered: int = 0) -> str:
    """Render one Anthropic request into the text agy is fed.

    Claude Code resends the whole transcript on every turn while the agy
    process remembers its own history, so only the new tail goes upstream —
    resending would duplicate the conversation in the model's context. System
    and tool protocol are re-sent each turn: they are cheap, and agy may compact
    its own history.
    """
    parts = []
    if system:
        parts.append(_text_of(system).strip())
    parts.append(_TOOL_PROTOCOL)
    tools_text = _tools_to_text(tools)
    if tools_text:
        parts.append(tools_text)

    history = list(messages or [])
    fresh = history[max(0, already_delivered):]
    if history and not any(_text_of(m.get("content")).strip() for m in fresh):
        # The caller replayed a transcript whose tail was already delivered, so
        # the "new" slice is empty. Sending system+protocol alone made the model
        # answer "ready for your request" — it was asked a question with no
        # question in it. Re-send the last real message instead.
        fresh = history[-1:]
    for message in fresh:
        text = _text_of(message.get("content")).strip()
        if not text:
            continue
        role = "User" if message.get("role") == "user" else "Assistant"
        parts.append("%s: %s" % (role, text))
    parts.append("Assistant:")
    return "\n\n".join(p for p in parts if p)


# ── tool-call parsing ───────────────────────────────────────────────────────

def _json_objects(text: str):
    """Yield balanced top-level JSON objects found in `text`.

    A regex cannot do this: `{"tool_call":{"input":{"path":"a"}}}` has nested
    braces, and `\\{[^{}]*` stops at the first inner `}` — json.loads then fails
    and the tool call silently degrades into plain text, which still looks like
    a successful turn to the caller.
    """
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    yield text[start:index + 1]
                    start = -1


def parse_tool_call(text: str):
    """Split model output into (text_before, tool_use blocks) or None."""
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
            "id": "toolu_agy_" + uuid.uuid4().hex[:20],
            "name": str(call["name"]),
            "input": call.get("input") if isinstance(call.get("input"), dict) else {},
        }
        return text[: text.index(raw)].strip(), [block]
    return None


def _blocks(text: str):
    parsed = parse_tool_call(text)
    if parsed is None:
        return [{"type": "text", "text": text}], "end_turn"
    before, tool_blocks = parsed
    blocks = []
    if before:
        blocks.append({"type": "text", "text": before})
    blocks.extend(tool_blocks)
    return blocks, "tool_use"


# ── the resident agy process ────────────────────────────────────────────────

class Conversation:
    """One long-lived `agy` process bound to one external conversation.

    stdin is a single NDJSON channel, so turns for a conversation are serialised
    by this lock. Separate conversations get separate processes, which is what
    keeps concurrent Claude Code sub-calls from interleaving.
    """

    def __init__(self, key: str, binary: str, model: str, cwd: str):
        self.key = key
        self.binary = binary
        self.model = model
        self.cwd = cwd
        self.lock = threading.Lock()
        self.last_used = time.time()
        self.conversation_id = None
        self.delivered = 0
        self.completed_turns = 0
        self._proc = None
        self._queue = None
        self._reader = None

    # -- process lifecycle ---------------------------------------------------

    def _spawn(self):
        argv = [
            self.binary,
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            # plan mode is what keeps agy from executing on this host; see the
            # module docstring for the measurement behind it.
            "--mode", "plan",
            "--model", self.model,
        ]
        _log("spawn: %s" % " ".join(argv))
        self._proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=self.cwd, text=True,
            encoding="utf-8", errors="replace", bufsize=1,
        )
        # A dedicated reader thread, not select(): readline() pulls a whole
        # chunk into Python's buffer, so a select() on the raw fd only ever
        # reports *new* bytes — events already buffered become invisible and the
        # turn then hangs until its timeout even though the answer arrived.
        self._queue = queue.Queue()
        stdout = self._proc.stdout

        def _pump():
            try:
                for line in stdout:
                    self._queue.put(line)
            except (ValueError, OSError):
                pass
            finally:
                self._queue.put(None)  # EOF sentinel

        self._reader = threading.Thread(target=_pump, daemon=True)
        self._reader.start()

    def _kill(self):
        proc = self._proc
        self._proc = None
        self._queue = None
        self._reader = None
        self.completed_turns = 0
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        except OSError as exc:  # pragma: no cover - platform dependent
            if exc.errno != errno.ESRCH:
                raise
        finally:
            # Reap unconditionally. The wait() above only runs on the branch
            # where the process was still alive when we got here; a child that
            # had ALREADY exited took the poll() is not None path, skipped the
            # wait, and was left as a zombie for as long as its conversation key
            # went untouched — which for a one-off request is forever. Observed
            # as `[agy] <defunct>` children reparented to the bridge process.
            try:
                if proc.stdout:
                    proc.stdout.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass
        _log("conversation %s: process reaped" % self.key[:8])

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self):
        with self.lock:
            self._kill()

    # -- protocol ------------------------------------------------------------

    def _read_line(self, budget: float, idle: float) -> str:
        """Next stdout line, or raise.

        Two ceilings: `idle` caps the gap between events (a turn that streams
        deltas for minutes is fine, one that goes silent is not) and `budget`
        caps the whole turn.
        """
        deadline = time.monotonic() + budget
        while True:
            remaining_idle = min(idle, deadline - time.monotonic())
            if remaining_idle <= 0:
                raise UpstreamError(
                    "agy produced no event within %ds" % int(idle),
                    status=504, kind="timeout_error")
            try:
                line = self._queue.get(timeout=remaining_idle)
            except queue.Empty:
                continue
            if line is None:
                raise UpstreamError(
                    "agy exited (code %s) mid-turn" % self._proc.poll(),
                    status=502, kind="api_error")
            return line

    def turn(self, prompt: str):
        """Run one turn, yielding ('text', delta) then ('result', payload).

        Generator-based so a streaming caller can forward deltas as they land
        instead of replaying one completed message at the end.
        """
        with self.lock:
            self.last_used = time.time()
            if not self.alive():
                if self._proc is not None:
                    _log("conversation %s: process was gone (exit=%s), respawning"
                         % (self.key[:8], self._proc.poll()))
                self._kill()
                self._spawn()
            first = self.completed_turns == 0

            request = {"event": "user",
                       "message": {"role": "user", "content": prompt}}
            line = json.dumps(request, ensure_ascii=False)
            _log("→ turn (%d chars) to %s%s"
                 % (len(line), self.model, " [first]" if first else ""))
            try:
                self._proc.stdin.write(line + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self._kill()
                raise UpstreamError("agy stdin closed: %s" % exc)

            budget = FIRST_TURN_TIMEOUT_S if first else TURN_TIMEOUT_S
            got_text = False
            text = []
            while True:
                raw = self._read_line(budget=budget, idle=IDLE_TIMEOUT_S).strip()
                if not raw or raw.startswith(":"):
                    continue  # heartbeat
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                kind = event.get("event")
                if kind == "init":
                    cid = event.get("conversation_id") or (
                        (event.get("init") or {}).get("conversation_id"))
                    if cid:
                        self.conversation_id = cid
                    continue
                if kind == "step_update":
                    delta = (event.get("step_update") or {}).get("text_delta")
                    if delta:
                        got_text = True
                        text.append(delta)
                        yield ("text", delta)
                    continue
                if kind == "result":
                    result = event.get("result") or {}
                    body = result.get("response") or "".join(text)
                    status = str(result.get("status") or "").upper()
                    detail = result.get("error") or ""
                    if not (body or "").strip():
                        # Say what actually happened. Reporting every empty body
                        # as "empty completion; retry" hid the real cause once
                        # already: a turn whose content tripped Gemini's safety
                        # filter comes back `status=SUCCESS, response=""` with the
                        # explanation in `error`, and a retry reproduces it
                        # forever because nothing about the request changed.
                        text_detail = ("%s %s" % (status, detail)).strip()
                        filtered = any(w in text_detail.lower() for w in (
                            "block", "filter", "safety", "policy", "prohibited"))
                        if filtered:
                            raise UpstreamError(
                                "agy rejected the turn on content grounds: %s"
                                % (detail or status),
                                status=400, kind="invalid_request_error")
                        if detail or (status and status != "SUCCESS"):
                            raise UpstreamError(
                                "agy turn failed (%s): %s" % (status or "no status",
                                                              detail or "no detail"),
                                status=502, kind="overloaded_error")
                        raise UpstreamError(
                            "agy returned an empty completion; retry the request",
                            status=502, kind="overloaded_error")
                    self.completed_turns += 1
                    self.last_used = time.time()
                    yield ("result", {
                        "text": body,
                        "usage": result.get("usage") or {},
                        "duration": result.get("duration_seconds"),
                    })
                    return

    def run(self, prompt: str):
        """Drain one turn and return the result payload."""
        for kind, payload in self.turn(prompt):
            if kind == "result":
                return payload
        raise UpstreamError("agy turn ended without a result event",
                            status=502, kind="api_error")


# ── conversation pool ───────────────────────────────────────────────────────

class Pool:
    """LRU of resident conversations, safe for concurrent callers."""

    def __init__(self, binary: str, cwd: str):
        self.binary = binary
        self.cwd = cwd
        self._lock = threading.Lock()
        self._entries = {}  # key -> [conversation, delivered]

    @staticmethod
    def key_for(model: str, system, messages) -> str:
        """Stable id for a conversation.

        Claude Code replays the transcript every turn, so the first user turn
        plus the system prompt identify the conversation across turns while
        staying distinct between separate ones.
        """
        first = ""
        for message in messages or []:
            if message.get("role") == "user":
                first = _text_of(message.get("content"))[:400]
                break
        seed = "%s\n%s\n%s" % (model, _text_of(system)[:400], first)
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]

    def get(self, key: str, model: str):
        doomed = []
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                convo = entry[0]
                if not convo.alive() and convo._proc is not None:
                    doomed.append(self._entries.pop(key)[0])
                    entry = None
            if entry is None:
                convo = Conversation(key, self.binary, model, self.cwd)
                self._entries[key] = [convo, 0]
                entry = self._entries[key]
                doomed.extend(self._evict_locked())
            convo = entry[0]
            convo.last_used = time.time()
        for stale in doomed:
            stale.close()
        return convo, entry

    def note_delivered(self, key: str, count: int):
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry[1] = count

    def delivered(self, key: str) -> int:
        with self._lock:
            entry = self._entries.get(key)
            return entry[1] if entry else 0

    def _evict_locked(self):
        """Drop idle/overflow conversations, returning them for closing.

        This runs while the pool lock is held and must NOT close inline:
        `Conversation.close()` takes that conversation's own lock, which a turn
        in flight holds for the whole request, so closing here would stall the
        entire pool until the model answered. The caller closes after releasing.

        It also used to call `.close()` on the `[convo, count]` entry rather
        than the conversation, which raised AttributeError in the middle of
        eviction and took unrelated in-flight requests down with it.
        """
        doomed = []
        now = time.time()
        for key, entry in list(self._entries.items()):
            if now - entry[0].last_used > SESSION_IDLE_TTL_S:
                doomed.append(self._entries.pop(key)[0])
        while len(self._entries) > MAX_SESSIONS:
            key, entry = min(self._entries.items(),
                             key=lambda kv: kv[1][0].last_used)
            doomed.append(self._entries.pop(key)[0])
        return doomed

    def close_all(self):
        with self._lock:
            for entry in self._entries.values():
                entry[0].close()
            self._entries.clear()


# ── HTTP ────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    pool: Pool = None  # type: ignore[assignment]
    server_version = "agy-bridge/1.0"
    _responded = False

    def log_message(self, fmt, *args):
        pass

    # -- helpers -------------------------------------------------------------

    def _reset(self):
        self._responded = False

    def _json(self, status: int, payload: dict):
        if self._responded:
            return
        self._responded = True
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    # -- endpoints -----------------------------------------------------------

    def do_GET(self):
        self._reset()
        path = urlparse(self.path).path
        if path == "/health":
            self._json(200, {"status": "ok", "provider": "agy",
                             "binary": self.pool.binary})
        elif path == "/v1/models":
            self._json(200, {"data": [
                {"id": name, "object": "model", "owned_by": "agy"}
                for name in sorted(AGY_MODELS)]})
        else:
            self._json(404, {"type": "error", "error": {
                "type": "not_found_error", "message": "no such path %s" % path}})

    def do_POST(self):
        self._reset()
        try:
            self._post()
        except Exception as exc:  # noqa: BLE001 - a handler must never drop the
            # connection: an unhandled exception here produced an *empty*
            # response with no error body, which reads to the caller exactly
            # like the provider silently failing.
            _log("handler crash on %s: %r" % (self.path, exc))
            self._json(500, {"type": "error", "error": {
                "type": "api_error",
                "message": "agy bridge internal error: %r" % (exc,)}})

    def _post(self):
        path = urlparse(self.path).path
        if path != "/v1/messages":
            self._json(404, {"type": "error", "error": {
                "type": "not_found_error", "message": "no such path %s" % path}})
            return
        try:
            request = self._read_body()
        except (json.JSONDecodeError, ValueError) as exc:
            self._json(400, {"type": "error", "error": {
                "type": "invalid_request_error",
                "message": "malformed JSON body: %s" % exc}})
            return
        try:
            model = resolve_model(request.get("model"))
        except UpstreamError as exc:
            self._json(exc.status, {"type": "error", "error": {
                "type": exc.kind, "message": str(exc)}})
            return
        t0 = time.monotonic()
        _log("← %s model=%s stream=%s messages=%d tools=%s"
             % (path, model, bool(request.get("stream")),
                len(request.get("messages") or []),
                len(request.get("tools") or [])))
        try:
            if request.get("stream"):
                self._stream(request, model)
            else:
                self._once(request, model)
        except Exception:
            _log("✗ request failed after %.1fs" % (time.monotonic() - t0))
            raise
        _log("✓ request done in %.1fs" % (time.monotonic() - t0))

    # -- turn plumbing -------------------------------------------------------

    def _prepare(self, request, model):
        messages = request.get("messages") or []
        key = Pool.key_for(model, request.get("system"), messages)
        convo, _ = self.pool.get(key, model)
        already = self.pool.delivered(key)
        prompt = build_prompt(request.get("system"), messages,
                              request.get("tools"), already)
        # Everything the caller sent is now upstream. Marking `len(messages) - 1`
        # (an earlier mistake) never advances for a single-message conversation,
        # so every turn resent the whole transcript and every turn looked like a
        # cold first turn — which is both the wrong prompt and the wrong timeout.
        self.pool.note_delivered(key, len(messages))
        return convo, prompt, key, already

    @staticmethod
    def _usage(usage, prompt, body):
        usage = usage or {}
        return {
            "input_tokens": int(usage.get("input_tokens")
                                or max(1, len(prompt) // 4)),
            "output_tokens": int(usage.get("output_tokens")
                                 or max(1, len(body) // 4)),
        }

    def _once(self, request, model):
        try:
            convo, prompt, key, already = self._prepare(request, model)
            payload = convo.run(prompt)
        except UpstreamError as exc:
            self._json(exc.status, {"type": "error", "error": {
                "type": exc.kind, "message": str(exc)}})
            return
        except BinaryMissing as exc:
            self._json(500, {"type": "error", "error": {
                "type": "api_error", "message": str(exc)}})
            return
        blocks, stop = _blocks(payload["text"])
        self._json(200, {
            "id": "msg_agy_" + uuid.uuid4().hex[:20],
            "type": "message", "role": "assistant", "model": model,
            "content": blocks, "stop_reason": stop, "stop_sequence": None,
            "usage": self._usage(payload.get("usage"), prompt, payload["text"]),
        })

    def _stream(self, request, model):
        try:
            convo, prompt, key, already = self._prepare(request, model)
        except BinaryMissing as exc:
            self._json(500, {"type": "error", "error": {
                "type": "api_error", "message": str(exc)}})
            return

        msg_id = "msg_agy_" + uuid.uuid4().hex[:20]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # Chunked framing lets a strict HTTP/1.1 client see the end of the body;
        # without it the client waits for an EOF that never arrives.
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self._responded = True
        self._chunked = True

        text = []
        usage = {}
        try:
            self._sse("message_start", {"type": "message_start", "message": {
                "id": msg_id, "type": "message", "role": "assistant",
                "model": model, "content": [], "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0}}})
            opened = False
            for kind, payload in convo.turn(prompt):
                if kind == "text":
                    if not opened:
                        self._sse("content_block_start", {
                            "type": "content_block_start", "index": 0,
                            "content_block": {"type": "text", "text": ""}})
                        opened = True
                    text.append(payload)
                    self._sse("content_block_delta", {
                        "type": "content_block_delta", "index": 0,
                        "delta": {"type": "text_delta", "text": payload}})
                elif kind == "result":
                    usage = payload.get("usage") or {}
            if not opened:
                self._sse("content_block_start", {
                    "type": "content_block_start", "index": 0,
                    "content_block": {"type": "text", "text": ""}})
            self._sse("content_block_stop", {"type": "content_block_stop",
                                             "index": 0})
            body = "".join(text)
            self._sse("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": self._usage(usage, prompt, body)})
            self._sse("message_stop", {"type": "message_stop"})
            self._chunked_end()
        except (UpstreamError, BrokenPipeError, ConnectionResetError, OSError) as exc:
            # Headers are already out, so the failure has to be reported
            # in-band; a second HTTP response would corrupt the stream.
            kind = getattr(exc, "kind", "api_error")
            self._sse("error", {"type": "error", "error": {
                "type": kind, "message": str(exc)}})
            try:
                self._sse("message_stop", {"type": "message_stop"})
                self._chunked_end()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    # -- chunked SSE ---------------------------------------------------------

    def _chunk_write(self, data: bytes):
        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
        self.wfile.flush()

    def _chunked_end(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()
        self.close_connection = True

    def _sse(self, event: str, payload: dict):
        line = "event: %s\ndata: %s\n\n" % (
            event, json.dumps(payload, ensure_ascii=False))
        self._chunk_write(line.encode("utf-8"))


# ── entry point ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--serve", action="store_true", help="run the server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    if not args.serve:
        parser.print_help()
        return

    try:
        binary = _agy_binary()
    except BinaryMissing as exc:
        _log("FATAL: %s" % exc)
        sys.exit(2)

    pool = Pool(binary, os.getcwd())
    Handler.pool = pool
    atexit.register(pool.close_all)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True

    def _shutdown(*_):
        try:
            pool.close_all()
        finally:
            os._exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    _log("Anthropic endpoint on http://%s:%d (agy=%s, mode=plan)"
         % (args.host, args.port, binary))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        pool.close_all()


if __name__ == "__main__":
    main()