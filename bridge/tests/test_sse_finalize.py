from sse_finalize import SseCutState, clamp_max_tokens, event_is_ping, resolve_max_output, sse_should_close, split_complete_events


def test_close_after_message_stop():
    assert sse_should_close(b"event: message_stop\ndata: {}\n\n") is True
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

