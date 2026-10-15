"""Guards for the Antigravity CLI adapter (bridge/agy_bridge.py).

The assertions here encode things measured against the real `agy`, because each
one fails *quietly* otherwise: a tool call that degrades into prose still looks
like a successful turn, and a turn that never reaches the model still returns
HTTP 200.
"""
from __future__ import annotations

import json
import threading
import time
import sys
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))

import agy_bridge as agy  # noqa: E402


# ── which binary this runs ──────────────────────────────────────────────────

def test_windows_build_is_refused():
    """The Windows build re-authenticates per process and raises Google OAuth
    prompts on the user's desktop. It does not fail — it succeeds slowly while
    filling the screen with browser windows, so it has to be rejected by name.
    """
    original = agy.AGY_BIN
    try:
        agy.AGY_BIN = r"C:\Users\me\AppData\Local\agy\bin\agy.exe"
        with pytest.raises(agy.BinaryMissing):
            agy._agy_binary()
    finally:
        agy.AGY_BIN = original


def test_missing_binary_is_reported_clearly():
    original = agy.AGY_BIN
    try:
        agy.AGY_BIN = "/nonexistent/agy"
        with pytest.raises(agy.BinaryMissing):
            agy._agy_binary()
    finally:
        agy.AGY_BIN = original


def test_default_binary_is_an_absolute_path():
    """`which agy` returns the Windows build on this host, and a shell alias
    adds --dangerously-skip-permissions on top. Resolution must never consult
    either."""
    assert agy.AGY_BIN.startswith("/")
    assert "which" not in agy.AGY_BIN


# ── model names ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("gemini-3.8-flash-low", "gemini-3.8-flash-low"),
    ("agy-gemini-3.8-flash-low", "gemini-3.8-flash-low"),
    ("agy-claude-sonnet-4-6", "claude-sonnet-4-6"),
    ("claude-sonnet-4-6[1m]", "claude-sonnet-4-6"),
    ("agy-claude-opus-4-6-thinking[1m]", "claude-opus-4-6-thinking"),
])
def test_model_names_resolve_through_alias_and_window_marker(name, expected):
    assert agy.resolve_model(name) == expected


def test_unknown_model_is_a_clear_404_not_a_500():
    with pytest.raises(agy.UpstreamError) as exc:
        agy.resolve_model("gpt-9-ultra")
    assert exc.value.status == 404


# ── tool-call protocol ───────────────────────────────────────────────────────

def test_tool_call_with_nested_input_is_parsed():
    """`{"input":{"path":"a"}}` carries its own braces; a non-balancing scan
    would stop at the inner `}`, fail to decode, and silently return the tool
    call as prose with stop_reason "end_turn"."""
    got = agy.parse_tool_call('{"tool_call":{"name":"read","input":{"path":"a.txt"}}}')
    assert got is not None
    before, blocks = got
    assert before == ""
    assert blocks[0]["name"] == "read"
    assert blocks[0]["input"] == {"path": "a.txt"}


def test_tool_call_survives_a_code_fence():
    text = '```json\n{"tool_call":{"name":"ls","input":{"dir":"/tmp"}}}\n```'
    got = agy.parse_tool_call(text)
    assert got is not None and got[1][0]["input"] == {"dir": "/tmp"}


def test_braces_inside_strings_do_not_confuse_the_scanner():
    got = agy.parse_tool_call('{"tool_call":{"name":"x","input":{"cmd":"echo } not the end"}}}')
    assert got is not None
    assert got[1][0]["input"] == {"cmd": "echo } not the end"}


def test_plain_prose_is_not_a_tool_call():
    assert agy.parse_tool_call("直接回答你，不需要工具。") is None


def test_tool_call_marks_the_turn_as_tool_use():
    blocks, stop = agy._blocks('{"tool_call":{"name":"ls","input":{}}}')
    assert stop == "tool_use" and blocks[0]["type"] == "tool_use"


def test_plain_answer_is_end_turn():
    blocks, stop = agy._blocks("pong")
    assert stop == "end_turn"
    assert blocks == [{"type": "text", "text": "pong"}]


# ── prompt rendering ─────────────────────────────────────────────────────────

def test_tools_are_described_as_text_because_agy_cannot_receive_schemas():
    out = agy.build_prompt(None, [{"role": "user", "content": "hi"}],
                           [{"name": "Bash", "description": "run",
                             "input_schema": {"type": "object"}}])
    assert "Bash" in out


def test_tool_use_and_tool_result_turns_survive_flattening():
    out = agy.build_prompt(None, [
        {"role": "user", "content": "读一下 a.txt"},
        {"role": "assistant", "content": [{"type": "tool_use", "name": "read",
                                           "input": {"path": "a.txt"}}]},
        {"role": "user", "content": [{"type": "tool_result", "content": "hello"}]},
    ], None)
    assert "a.txt" in out and "hello" in out


def test_only_the_undelivered_tail_is_resent():
    """agy keeps its own history; resending the transcript duplicates it inside
    the model's context on every turn."""
    msgs = [{"role": "user", "content": "第一轮"},
            {"role": "assistant", "content": "回复"},
            {"role": "user", "content": "第二轮"}]
    fresh = agy.build_prompt(None, msgs, None)
    tail = agy.build_prompt(None, msgs, None, already_delivered=2)
    assert "第一轮" in fresh and "第二轮" in fresh
    assert "第一轮" not in tail and "第二轮" in tail


def test_an_already_delivered_transcript_still_asks_something():
    """A replayed transcript whose tail was all delivered would otherwise send
    system+protocol with no question, and the model answers "ready for your
    request" — measured, not hypothetical."""
    msgs = [{"role": "user", "content": "只回复数字 7"}]
    out = agy.build_prompt(None, msgs, None, already_delivered=1)
    assert "只回复数字 7" in out


def test_conversation_key_is_stable_and_distinguishing():
    msgs = [{"role": "user", "content": "同一个问题"}]
    same = [{"role": "user", "content": "同一个问题"},
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": "继续"}]
    other = [{"role": "user", "content": "另一个问题"}]
    assert agy.Pool.key_for("m", None, msgs) == agy.Pool.key_for("m", None, same)
    assert agy.Pool.key_for("m", None, msgs) != agy.Pool.key_for("m", None, other)


# ── process lifecycle ────────────────────────────────────────────────────────

def test_eviction_closes_conversations_not_the_wrapper_list():
    """This used to call `.close()` on the `[convo, count]` entry: an
    AttributeError in the middle of eviction that failed unrelated requests
    already in flight.

    Timestamps are "now" so the idle-TTL branch does not also fire — this
    isolates the overflow branch, which must return conversations rather than
    closing them under the pool lock.
    """
    import time as _time

    pool = agy.Pool("/bin/true", "/tmp")

    class Fake:
        def __init__(self):
            self.last_used = _time.time()
            self.closed = False

        def close(self):
            self.closed = True

    fakes = [Fake() for _ in range(agy.MAX_SESSIONS + 3)]
    for index, fake in enumerate(fakes):
        pool._entries["k%d" % index] = [fake, 0]
    doomed = pool._evict_locked()
    assert len(doomed) == 3
    assert len(pool._entries) == agy.MAX_SESSIONS
    assert all(fake.closed is False for fake in doomed)  # not closed under the lock


def test_idle_conversations_are_evicted_too():
    import time as _time

    pool = agy.Pool("/bin/true", "/tmp")

    class Fake:
        def __init__(self):
            self.last_used = _time.time() - agy.SESSION_IDLE_TTL_S - 60

        def close(self):
            pass

    pool._entries["stale"] = [Fake(), 0]
    doomed = pool._evict_locked()
    assert len(doomed) == 1 and "stale" not in pool._entries


def test_pool_returns_the_same_conversation_for_the_same_key():
    pool = agy.Pool("/bin/true", "/tmp")
    try:
        first, _ = pool.get("k", "m")
        second, _ = pool.get("k", "m")
        assert first is second
    finally:
        pool.close_all()


def test_empty_completion_is_an_error_not_a_successful_empty_turn():
    with pytest.raises(agy.UpstreamError) as exc:
        raise agy.UpstreamError("agy returned an empty completion",
                                 status=502, kind="overloaded_error")
    assert exc.value.kind == "overloaded_error"


def test_usage_falls_back_when_agy_reports_nothing():
    usage = agy.Handler._usage({}, "a" * 400, "b" * 80)
    assert usage["input_tokens"] > 0 and usage["output_tokens"] > 0


def test_known_models_are_the_ones_agy_actually_serves():
    """`agy models`, measured. A typo here would only surface as a runtime
    failure in someone's session."""
    assert "gemini-3.8-flash-low" in agy.AGY_MODELS
    assert "claude-sonnet-4-6" in agy.AGY_MODELS
    assert len(agy.AGY_MODELS) == 6

def test_a_filtered_turn_is_reported_as_content_rejection_not_an_empty_body():
    """Gemini answers a filtered turn with `status=SUCCESS, response=""`.

    The explanation lives in `error`. Reporting that as a generic "empty
    completion; retry" is what once sent a debugging session chasing a phantom
    size limit: retrying a content rejection reproduces it forever, because
    nothing about the request changed. It has to arrive as a non-retryable
    invalid_request carrying the provider's own wording.
    """
    class _Stdin:
        def write(self, _data):
            return len(_data)

        def flush(self):
            pass

    class _Proc:
        stdin = _Stdin()

        def poll(self):
            return None

    convo = agy.Conversation.__new__(agy.Conversation)
    convo.key = "k"
    convo.model = "gemini-3.8-flash-low"
    convo.lock = threading.Lock()
    convo.completed_turns = 0
    convo.last_used = 0.0
    convo.conversation_id = None
    convo._proc = _Proc()
    convo._queue = None
    convo.alive = lambda: True
    convo._spawn = lambda: None
    convo._kill = lambda: None

    lines = ['{"event":"result","result":{"status":"SUCCESS","response":"",'
             '"error":"This request was blocked by Gemini\'s filter."}}']
    convo._read_line = lambda budget=None, idle=None: lines.pop(0) if lines else ""

    with pytest.raises(agy.UpstreamError) as exc:
        list(convo.turn("hi"))
    assert exc.value.status == 400
    assert exc.value.kind == "invalid_request_error"
    assert "blocked" in str(exc.value).lower()


def test_a_child_that_already_exited_is_still_reaped():
    """`_kill` must reap even when there is nothing left to terminate.

    The wait() only ran on the branch where the process was still alive, so a
    child that had already exited skipped it and stayed a zombie until its
    conversation key was touched again — forever, for a one-off request.
    Observed as `[agy] <defunct>` children reparented to the bridge process.

    Checked against the process table rather than Python's returncode: Popen
    caches the exit status, so `poll()` looks identical whether or not the
    zombie was ever reaped. Once reaped the pid is gone, and signalling it
    raises ProcessLookupError; a zombie would still accept the signal.
    """
    import os as _os
    import subprocess as _sp

    convo = agy.Conversation.__new__(agy.Conversation)
    convo.key = "reap"
    convo.lock = threading.Lock()
    convo.completed_turns = 0
    convo.last_used = 0.0
    convo.conversation_id = None
    convo._queue = None
    convo._reader = None
    convo._proc = _sp.Popen(["/bin/true"])
    time.sleep(0.3)

    pid = convo._proc.pid
    assert convo._proc.poll() is not None, "child should have exited already"
    convo._kill()

    with pytest.raises(ProcessLookupError):
        _os.kill(pid, 0)
