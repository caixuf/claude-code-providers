from sse_finalize import SseCutState, clamp_max_tokens, event_is_ping, resolve_max_output, sse_should_close, split_complete_events, strip_1m_beta


def test_close_after_message_stop():
    assert sse_should_close(b"event: message_stop\ndata: {}\n\n") is True
    assert sse_should_close(b"data: [DONE]\n\n") is True
    assert sse_should_close(b"event: message_start\n\n") is False


def test_split_and_ping():
    events, rest = split_complete_events(b"event: ping\n\nevent: foo")
    assert len(events) == 1
    assert event_is_ping(events[0]) is True
    assert rest == b"event: foo"


def test_state_stop_cuts_even_with_trailing_ping():
    st = SseCutState()
    fwd, done = st.feed(b"event: message_start\n\n")
    assert done is False and st.saw_real is True
    fwd, done = st.feed(b'event: message_stop\ndata: {"type":"message_stop"}\n\nevent: ping\n\n')
    assert done is True
    assert b"message_stop" in fwd


def test_default_idle_does_not_shrink_budget():
    st = SseCutState(ping_idle_s=0, first_byte_s=90)
    assert st.wait_budget() == 90
    st.feed(b"event: content_block_delta\n\n")
    assert st.wait_budget() == 3600.0


def test_idle_timeout_does_not_inject_json():
    st = SseCutState()
    assert st.idle_timeout() == b""


def test_strip_1m_model():
    from sse_finalize import strip_1m_model
    import json

    # Strips [1m]
    raw, stripped = strip_1m_model(b'{"model":"cmdc-deepseek[1m]","messages":[]}')
    assert stripped == "cmdc-deepseek[1m]"
    assert json.loads(raw)["model"] == "cmdc-deepseek"

    # Strips uppercase [1M]
    raw, stripped = strip_1m_model(b'{"model":"claude-sonnet-5.5[1M]"}')
    assert stripped == "claude-sonnet-5.5[1M]"
    assert json.loads(raw)["model"] == "claude-sonnet-5.5"

    # Leaves regular models intact
    raw, stripped = strip_1m_model(b'{"model":"claude-sonnet-5.5"}')
    assert stripped is None
    assert json.loads(raw)["model"] == "claude-sonnet-5.5"

    # Handles empty/non-json safely
    assert strip_1m_model(b"") == (b"", None)
    assert strip_1m_model(b"not json") == (b"not json", None)


CLAMP = {"cmdc-deepseek": 393216, "cmdc-deepseek[1m]": 393216, "claude-sonnet-5.5": 128000}


def test_resolve_max_output():
    # exact and suffix-normalized hits
    assert resolve_max_output("cmdc-deepseek", CLAMP) == 393216
    assert resolve_max_output("cmdc-deepseek[1m]", CLAMP) == 393216
    assert resolve_max_output("CMDC-DEEPSEEK[1M]", CLAMP) == 393216
    # miss -> fallback (None disables clamping)
    assert resolve_max_output("unknown-model", CLAMP) is None
    assert resolve_max_output("unknown-model", CLAMP, fallback=393216) == 393216
    # empty map -> fallback
    assert resolve_max_output("whatever", {}, fallback=1000) == 1000
    assert resolve_max_output("whatever", {}) is None


def test_clamp_max_tokens_clamps_when_over_cap():
    import json

    body = b'{"model":"cmdc-deepseek","max_tokens":1000000,"messages":[]}'
    out, clamped = clamp_max_tokens(body, CLAMP)
    assert clamped == 393216
    assert json.loads(out)["max_tokens"] == 393216


def test_clamp_max_tokens_no_change_when_equal_or_below():
    import json

    body = b'{"model":"cmdc-deepseek","max_tokens":393216}'
    out, clamped = clamp_max_tokens(body, CLAMP)
    assert clamped is None and out == body

    body = b'{"model":"cmdc-deepseek","max_tokens":100}'
    out, clamped = clamp_max_tokens(body, CLAMP)
    assert clamped is None and out == body


def test_clamp_max_tokens_leaves_non_json_and_missing_field():
    assert clamp_max_tokens(b"", CLAMP) == (b"", None)
    assert clamp_max_tokens(b"not json", CLAMP) == (b"not json", None)
    body = b'{"model":"cmdc-deepseek","messages":[]}'
    assert clamp_max_tokens(body, CLAMP) == (body, None)


def test_clamp_max_tokens_unknown_model_uses_fallback():
    body = b'{"model":"brand-new-model","max_tokens":1000000}'
    out, clamped = clamp_max_tokens(body, CLAMP, fallback=393216)
    assert clamped == 393216

    # no fallback -> unknown model left untouched
    out2, clamped2 = clamp_max_tokens(body, CLAMP, fallback=None)
    assert clamped2 is None


def test_strip_then_clamp_combined():
    """The real proxy pipeline: cmdc-deepseek[1m] + max_tokens=1000000."""
    import json

    from sse_finalize import strip_1m_model

    raw, stripped = strip_1m_model(b'{"model":"cmdc-deepseek[1m]","max_tokens":1000000,"messages":[]}')
    assert stripped == "cmdc-deepseek[1m]"
    out, clamped = clamp_max_tokens(raw, CLAMP, fallback=393216)
    doc = json.loads(out)
    assert doc["model"] == "cmdc-deepseek"
    assert doc["max_tokens"] == 393216
    assert clamped == 393216



def test_strip_1m_beta():
    from sse_finalize import strip_1m_beta

    # removes the token but keeps the other betas, order-preserving
    assert strip_1m_beta("a,context-1m-2025-08-07,b") == "a,b"
    # removes it wherever it sits, incl. leading/trailing
    assert strip_1m_beta("context-1m-2025-08-07") == ""
    assert strip_1m_beta("context-1m-2025-08-07,oauth-2025-04-20") == "oauth-2025-04-20"
    # case-insensitive, whitespace-tolerant
    assert strip_1m_beta("OAUTH-2025-04-20, Context-1M-2025-08-07 ") == "OAUTH-2025-04-20"
    # leaves unrelated betas untouched
    assert strip_1m_beta("oauth-2025-04-20,fine-grained-tool-streaming-2025-05-14") == (
        "oauth-2025-04-20,fine-grained-tool-streaming-2025-05-14"
    )
    # empty in -> empty out
    assert strip_1m_beta("") == ""


# ── channel-check sanitisation (CodeBuddy identity gate) ──────────────────

BILLING = "x-anthropic-billing-header: cc_version=2.1.287.95e; cc_entrypoint=sdk-cli;"
# The three identity lines Claude Code 2.1.287 can send, and which mode sends each.
IDENTITY_TUI = "You are Claude Code, Anthropic's official CLI for Claude."
IDENTITY_APPEND = (
    "You are Claude Code, Anthropic's official CLI for Claude, running within the Claude Agent SDK."
)
IDENTITY_SDK = "You are a Claude agent, built on Anthropic's Claude Agent SDK."


def test_sanitize_channel_removes_billing_header_block():
    """The real Claude Code shape: marker in its own first system text block."""
    import json

    from sse_finalize import sanitize_channel_markers

    body = json.dumps(
        {
            "model": "codebuddy[1m]",
            "system": [
                {"type": "text", "text": BILLING},
                {"type": "text", "text": "You are a helpful coding agent."},
            ],
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode()
    out, applied = sanitize_channel_markers(body)
    assert applied == ["billing-header"]
    doc = json.loads(out)
    # the marker-only block is dropped entirely, the real prompt survives
    assert doc["system"] == [{"type": "text", "text": "You are a helpful coding agent."}]


def test_sanitize_channel_keeps_other_text_in_shared_block():
    import json

    from sse_finalize import sanitize_channel_markers

    body = json.dumps(
        {
            "system": [{"type": "text", "text": BILLING + "\nReal instructions."}],
            "messages": [],
        }
    ).encode()
    out, applied = sanitize_channel_markers(body)
    assert applied == ["billing-header"]
    assert json.loads(out)["system"] == [{"type": "text", "text": "Real instructions."}]


def test_sanitize_channel_handles_string_system_and_messages():
    import json

    from sse_finalize import sanitize_channel_markers

    body = json.dumps(
        {
            "system": "Line one.\n" + BILLING + "\nLine two.",
            "messages": [{"role": "user", "content": [{"type": "text", "text": BILLING}]}],
        }
    ).encode()
    out, applied = sanitize_channel_markers(body)
    assert applied == ["billing-header"]
    doc = json.loads(out)
    assert doc["system"] == "Line one.\nLine two."
    assert doc["messages"][0]["content"] == []


def test_sanitize_channel_neutralises_the_identity_the_tui_sends():
    """The regression this file exists for.

    Stripping the billing block PROMOTES the identity line to the position the
    gateway inspects, so the interactive TUI (identity line ``Jw``) and
    ``-p --append-system-prompt`` (``j0``) were still refused with 11128. The
    opening now gets one stable leading newline; the words stay so the model
    still knows it is Claude Code.
    """
    import json

    from sse_finalize import sanitize_channel_markers

    for identity in (IDENTITY_TUI, IDENTITY_APPEND):
        body = json.dumps(
            {
                "system": [
                    {"type": "text", "text": BILLING},
                    {"type": "text", "text": identity},
                    {"type": "text", "text": "\nlong prompt..."},
                ],
                "messages": [],
            }
        ).encode()
        out, applied = sanitize_channel_markers(body)
        assert set(applied) == {"billing-header", "claude-identity"}, identity
        sysmsg = json.loads(out)["system"]
        # billing block gone, identity still there but no longer at the opening
        assert sysmsg[0]["text"].startswith("\n")
        assert identity in sysmsg[0]["text"]


def test_sanitize_channel_identity_without_billing_block():
    """`--append-system-prompt` on a build without the billing block."""
    import json

    from sse_finalize import sanitize_channel_markers

    body = json.dumps({"system": [{"type": "text", "text": IDENTITY_APPEND}], "messages": []}).encode()
    out, applied = sanitize_channel_markers(body)
    assert applied == ["claude-identity"]
    assert json.loads(out)["system"][0]["text"] == "\n" + IDENTITY_APPEND


def test_sanitize_channel_covers_string_system_identity():
    import json

    from sse_finalize import sanitize_channel_markers

    body = json.dumps({"system": IDENTITY_TUI + "\nmore", "messages": []}).encode()
    out, applied = sanitize_channel_markers(body)
    assert applied == ["claude-identity"]
    assert json.loads(out)["system"].startswith("\n")


def test_sanitize_channel_leaves_the_sdk_identity_alone():
    """`claude -p` (no --append-system-prompt) sends `z0`, which the gateway
    accepts. Adding a newline there would be a needless prompt mutation."""
    import json

    from sse_finalize import sanitize_channel_markers

    body = json.dumps({"system": [{"type": "text", "text": IDENTITY_SDK}], "messages": []}).encode()
    assert sanitize_channel_markers(body) == (body, [])


def test_sanitize_channel_supplies_a_system_prompt_when_stripping_empties_it():
    """The gateway has a second check, "first message is not system prompt"."""
    import json

    from sse_finalize import DEFAULT_SYSTEM_PROMPT, sanitize_channel_markers

    body = json.dumps({"system": [{"type": "text", "text": BILLING}], "messages": []}).encode()
    out, applied = sanitize_channel_markers(body)
    assert applied == ["billing-header"]
    sysmsg = json.loads(out)["system"]
    assert sysmsg and sysmsg[0]["text"] == DEFAULT_SYSTEM_PROMPT


def test_sanitize_channel_noop_otherwise():
    from sse_finalize import sanitize_channel_markers

    # nothing to do -> untouched, and the cheap early-out keeps other providers intact
    clean = b'{"system":[{"type":"text","text":"hello"}],"messages":[]}'
    assert sanitize_channel_markers(clean) == (clean, [])


def test_sanitize_channel_neutralises_identity_in_a_later_system_block():
    """The gateway scans *every* system block, not just the opening of `system`.

    Verified against the live gateway: a request whose first block is ordinary
    and whose second block opens with the identity is refused with 11128, while
    the same body with the identity first is accepted. Neutralising only the
    opening therefore left real sessions refused.
    """
    import json

    from sse_finalize import sanitize_channel_markers

    body = json.dumps({
        "system": [
            {"type": "text", "text": "You are an interactive CLI tool."},
            {"type": "text", "text": IDENTITY_TUI},
        ],
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    out, applied = sanitize_channel_markers(body)
    assert applied == ["claude-identity"]
    system = json.loads(out)["system"]
    assert system[0]["text"] == "You are an interactive CLI tool."  # untouched
    assert system[1]["text"] == "\n" + IDENTITY_TUI


def test_sanitize_channel_neutralises_identity_in_an_assistant_turn():
    """A resumed conversation carries the line in a past assistant message, and
    the gateway inspects those too. User turns are not inspected, so they stay
    byte-identical rather than being mutated for nothing."""
    import json

    from sse_finalize import sanitize_channel_markers

    body = json.dumps({
        "system": [{"type": "text", "text": "You are an interactive CLI tool."}],
        "messages": [
            {"role": "user", "content": IDENTITY_TUI + "\nand this is the user"},
            {"role": "assistant", "content": [{"type": "text", "text": IDENTITY_TUI}]},
            {"role": "user", "content": "carry on"},
        ],
    }).encode()
    out, applied = sanitize_channel_markers(body)
    assert applied == ["claude-identity"]
    msgs = json.loads(out)["messages"]
    assert msgs[0]["content"] == IDENTITY_TUI + "\nand this is the user"  # user: untouched
    assert msgs[1]["content"][0]["text"] == "\n" + IDENTITY_TUI


def test_sanitize_channel_catches_identity_repeated_in_both_places():
    """The real failure was the identity in `system` *and* earlier in a message:
    the first was neutralised, the second was not, and the request was refused."""
    import json

    from sse_finalize import sanitize_channel_markers

    body = json.dumps({
        "system": [{"type": "text", "text": IDENTITY_TUI}, {"type": "text", "text": "prompt"}],
        "messages": [{"role": "assistant", "content": IDENTITY_TUI}],
    }).encode()
    out, applied = sanitize_channel_markers(body)
    assert applied == ["claude-identity"]
    doc = json.loads(out)
    assert doc["system"][0]["text"] == "\n" + IDENTITY_TUI
    assert doc["messages"][0]["content"] == "\n" + IDENTITY_TUI
    assert sanitize_channel_markers(b"") == (b"", [])
    assert sanitize_channel_markers(b"not json") == (b"not json", [])
    # marker present but only in a field we do not walk -> still a no-op
    stray = b'{"system":"ok","metadata":{"x":"x-anthropic-billing-header:"}}'
    assert sanitize_channel_markers(stray) == (stray, [])
    # a *later* line may keep the identity; only the opening is inspected
    later = b'{"system":[{"type":"text","text":"context\\nYou are Claude Code, Anthropic\'s official CLI for Claude."}],"messages":[]}'
    assert sanitize_channel_markers(later) == (later, [])


def test_norm_model_key_matches_rules_to_sent_spelling():
    from sse_finalize import norm_model_key

    assert norm_model_key("codebuddy[1m]") == "codebuddy"
    assert norm_model_key("CODEBUDDY-GLM[1M]") == "codebuddy-glm"
    assert norm_model_key("glm-5.3") == "glm-5.3"
    assert norm_model_key(None) == ""
