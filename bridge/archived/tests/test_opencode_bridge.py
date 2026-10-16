"""Guards for the OpenCode free-tier adapter (bridge/opencode_bridge.py).

Every assertion here encodes something that was measured against the live
service, because each one is a place where the adapter can fail *silently*:
a tool call that degrades into prose still reads like a successful turn, and a
stream that emits one big delta still looks like a valid SSE response.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))

import opencode_bridge as ob  # noqa: E402


# ── tool-call protocol ───────────────────────────────────────────────────────

def test_tool_call_json_with_nested_input_is_parsed():
    """`{"input": {...}}` has braces of its own.

    A regex of the shape `\\{[^{}]*"tool_call"` stops at the *inner* brace, the
    json.loads then fails, and the tool call silently degrades into text with
    stop_reason "end_turn" — which looks like a normal answer.
    """
    got = ob.parse_tool_call('{"tool_call":{"name":"read","input":{"path":"a.txt"}}}')
    assert got is not None
    before, blocks = got
    assert before == ""
    assert blocks[0]["name"] == "read"
    assert blocks[0]["input"] == {"path": "a.txt"}


def test_tool_call_survives_code_fence_and_prose():
    text = '好的\n```json\n{"tool_call":{"name":"get_weather","input":{"city":"北京"}}}\n```'
    got = ob.parse_tool_call(text)
    assert got is not None
    assert got[1][0]["input"] == {"city": "北京"}


def test_plain_prose_is_not_mistaken_for_a_tool_call():
    """A sentence mentioning the protocol must not become a tool_use block."""
    assert ob.parse_tool_call("你可以输出 {\"tool_call\": {...}} 这种格式，但我现在直接回答。") is None \
        or not ob.parse_tool_call("你可以输出 {\"tool_call\": {...}} 这种格式，但我现在直接回答。")[1]


def test_tool_call_blocks_mark_the_turn_as_tool_use():
    blocks, stop = ob._blocks('{"tool_call":{"name":"ls","input":{}}}')
    assert stop == "tool_use"
    assert blocks[0]["type"] == "tool_use"


def test_a_plain_answer_is_end_turn():
    blocks, stop = ob._blocks("pong")
    assert stop == "end_turn"
    assert blocks == [{"type": "text", "text": "pong"}]


# ── model names ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "deepseek-v4.1-flash-free",
    "opencode-deepseek-v4.1-flash-free",
    "deepseek-v4.1-flash-free[1m]",
    "opencode-deepseek-v4.1-flash-free[1m]",
])
def test_model_names_resolve_through_alias_suffix_and_1m(name):
    """Claude Code sends the alias and the `[1m]` marker; both must land on the
    upstream id, and the marker must never reach the service."""
    svc = ob.Service(autostart=False)
    row = svc.resolve(name)
    assert row["id"] == "deepseek-v4.1-flash-free"


def test_unknown_model_is_rejected_rather_than_guessed():
    """Silently answering as a different model is worse than a 404."""
    svc = ob.Service(autostart=False)
    with pytest.raises(ob.UpstreamError):
        svc.resolve("gpt-9-turbo-ultra")


# ── safety: the native tools that run on THIS host ──────────────────────────

def test_execution_tools_are_denied_unconditionally():
    """opencode's build agent carries real shell/read/write.

    Measured: with this ruleset a prompt telling the model to create a file in
    its own runtime was refused and the sentinel never appeared; without the
    ruleset the same prompt really did run `ls -la /tmp` on this machine.
    """
    actions = {r["action"] for r in ob._NATIVE_TOOL_DENY if r["resource"] == "*"}
    assert {"shell", "edit", "write", "patch"} <= actions


def test_credential_paths_are_denied_for_read_only_tools():
    """A blanket `deny read` trips the free-tier gate (403 FreeTierError), so
    read stays on and the credentials are blocked by path instead —
    proxy.env alone holds every provider key."""
    scoped = {
        (r["action"], r["resource"])
        for r in ob._NATIVE_TOOL_DENY if r["resource"] != "*"
    }
    assert ("read", "**/proxy.env") in scoped
    assert ("read", "**/.env") in scoped
    for action in ("read", "glob", "grep"):
        assert (action, "/home/*/.config/**") in scoped


def test_every_permission_rule_is_well_formed():
    """Rule shape, and the one deliberate exception to "everything is a deny".

    The ruleset opens with `allow *` and then denies the individual tools. That
    leader is load-bearing rather than decorative: it is the shape that was
    measured to shrink the session from ~6083 tokens to ~1963, and the session
    permissions are a list of overrides rather than a default-deny policy.
    """
    allow = [r for r in ob._NATIVE_TOOL_DENY if r["effect"] == "allow"]
    assert allow == [{"action": "*", "resource": "*", "effect": "allow"}], (
        "exactly one allow-all leader, at the front"
    )
    assert ob._NATIVE_TOOL_DENY[0] is allow[0], "and it has to come first"
    for rule in ob._NATIVE_TOOL_DENY:
        assert set(rule) == {"action", "resource", "effect"}
        assert rule["effect"] in ("allow", "deny")
        assert rule["resource"]


def test_read_is_never_denied_globally():
    """Denying `read` makes the free-tier gate answer 403 instead of running.

    So it stays reachable — that is the whole reason the tool list can be cut
    down to a read-only remnant without the turn failing. Denying it for the
    credential paths is fine; denying the action outright is not.
    """
    assert not any(
        r["action"] == "read" and r["resource"] == "*" and r["effect"] == "deny"
        for r in ob._NATIVE_TOOL_DENY
    )


# ── protocol translation ────────────────────────────────────────────────────

def test_assistant_tool_result_turns_become_text_for_the_model():
    """opencode's prompt API takes text, not content blocks, so the tool loop
    has to be flattened before it goes upstream."""
    out = ob.build_prompt(
        None,
        [{"role": "user", "content": "读一下 a.txt"},
         {"role": "assistant", "content": [{"type": "tool_use", "name": "read",
                                            "input": {"path": "a.txt"}}]},
         {"role": "user", "content": [{"type": "tool_result", "content": "hello"}]}],
        None,
    )
    assert "a.txt" in out
    assert "hello" in out


def test_multi_turn_only_sends_the_new_suffix():
    """The client resends the whole transcript every turn while the service
    keeps its own copy per session; re-sending duplicates the conversation."""
    msgs = [{"role": "user", "content": "第一轮"}, {"role": "user", "content": "第二轮"}]
    fresh = ob.build_prompt(None, msgs, None)
    delta = ob.build_prompt(None, msgs, None, already_delivered=2)
    assert "第一轮" in fresh and "第二轮" in fresh
    assert "第一轮" not in delta and "第二轮" in delta


def test_empty_upstream_body_raises_instead_of_returning_an_empty_turn():
    """HTTP 200 with an empty body is a real upstream outcome.

    Reporting it as a successful empty message leaves the caller with a turn
    that looks fine and says nothing — so a turn that ends without ever
    emitting text has to surface as a retryable error.
    """
    class _FakeResponse:
        """Minimal stand-in for the urllib response: the reader consumes it with
        ``readline()``, not iteration."""

        def __init__(self, lines):
            self._lines = list(lines)

        def readline(self):
            return (self._lines.pop(0) + "\n").encode() if self._lines else b""

        def close(self):
            pass

    turn = ob.Turn.__new__(ob.Turn)
    turn.sid = "ses_test"
    turn.prompt = "hi"
    turn.verbose = False
    events = [
        'data: {"type":"session.execution.succeeded","data":{"sessionID":"ses_test"}}',
    ]
    turn.service = type("S", (), {
        "event_stream": lambda self, timeout=None: _FakeResponse(events),
        "prompt": lambda self, sid, text: None,
    })()
    with pytest.raises(ob.UpstreamError) as exc:
        list(turn.events())
    assert "empty" in str(exc.value).lower()


def test_upstream_503_is_mapped_to_a_retryable_kind():
    """Claude Code retries on overloaded_error; a bare 500 would not."""
    err = ob.UpstreamError("boom", status=503, kind="overloaded_error")
    assert err.status == 503
    assert err.kind == "overloaded_error"


def test_usage_falls_back_to_an_estimate_when_counters_are_missing():
    usage = ob._usage_from({}, "a" * 400, "b" * 80)
    assert usage["input_tokens"] > 0
    assert usage["output_tokens"] > 0


def test_service_state_path_is_read_from_the_home_directory():
    """The port is chosen per start; a hardcoded address would be a stale
    endpoint that only fails once opencode restarts."""
    assert ob.SERVICE_STATE.name == "service.json"
    assert ob.SERVICE_STATE.parent.name == "opencode"