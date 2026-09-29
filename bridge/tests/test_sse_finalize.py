from sse_finalize import SseCutState, event_is_ping, sse_should_close, split_complete_events


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
    fwd, done = st.feed(b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\nevent: ping\n\n")
    assert done is True
    assert b"message_stop" in fwd


def test_wait_budget_shrinks_after_real_event():
    st = SseCutState(ping_idle_s=12, first_byte_s=90)
    assert st.wait_budget() == 90
    st.feed(b"event: content_block_delta\n\n")
    assert st.wait_budget() == 12
