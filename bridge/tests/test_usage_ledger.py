"""Pin the parse and the ledger's durability rules.

The numbers below are the shape Anthropic actually sends: `message_start`
carries the input side, the closing `message_delta` carries the output side,
and neither alone is the answer.
"""

import json
import os

from usage_ledger import (
    Usage,
    UsageLedger,
    UsageTally,
    aggregate,
    read_ledger,
    usage_from_obj,
)


def _start(inp=120, cache_read=9000, cache_write=0, model="DeepSeek-V4.1-Flash"):
    return (
        "event: message_start\n"
        'data: {"type":"message_start","message":{"model":"'
        + model
        + '","usage":{"input_tokens":'
        + str(inp)
        + ',"output_tokens":1,"cache_read_input_tokens":'
        + str(cache_read)
        + ',"cache_creation_input_tokens":'
        + str(cache_write)
        + "}}}\n\n"
    ).encode()


def _delta(out=340):
    return (
        "event: message_delta\n"
        'data: {"type":"message_delta","usage":{"output_tokens":'
        + str(out)
        + "}}\n\n"
    ).encode()


def test_stream_needs_both_events():
    t = UsageTally()
    t.feed(_start())
    # message_start's output_tokens is a placeholder, not the answer
    assert t.usage.output_tokens == 1
    assert t.usage.input_tokens == 120
    t.feed(_delta())
    assert t.usage.output_tokens == 340
    # the input side survives the delta, which never repeats it
    assert t.usage.input_tokens == 120
    assert t.usage.cache_read_input_tokens == 9000
    assert t.model == "DeepSeek-V4.1-Flash"


def test_partial_event_is_not_parsed():
    """A chunk ending mid-JSON must not report a partial count."""
    t = UsageTally()
    whole = _delta(340)
    cut = whole.index(b'"output_tokens":') + 5
    t.feed(whole[:cut])
    assert t.usage.output_tokens == 0
    t.feed(whole[cut:])
    assert t.usage.output_tokens == 340


def test_events_split_across_arbitrary_boundaries():
    """Byte-at-a-time feeding gives the same total as one whole chunk."""
    payload = _start() + b"event: ping\n\n" + _delta()
    one = UsageTally()
    one.feed(payload)
    many = UsageTally()
    for i in range(len(payload)):
        many.feed(payload[i : i + 1])
    assert many.usage.output_tokens == one.usage.output_tokens == 340
    assert many.usage.input_tokens == one.usage.input_tokens == 120


def test_cumulative_delta_replaces_not_sums():
    """message_delta.usage is cumulative: three deltas of 100/200/340 is 340."""
    t = UsageTally()
    t.feed(_start())
    for n in (100, 200, 340):
        t.feed(_delta(n))
    assert t.usage.output_tokens == 340


def test_crlf_framing():
    t = UsageTally()
    t.feed(_start().replace(b"\n\n", b"\r\n\r\n"))
    t.feed(_delta().replace(b"\n\n", b"\r\n\r\n"))
    assert t.usage.output_tokens == 340


def test_non_stream_reply():
    body = json.dumps(
        {
            "model": "MiniMax-M3.1-Flash",
            "usage": {"input_tokens": 50, "output_tokens": 70},
        }
    ).encode()
    t = UsageTally()
    t.absorb_json(body)
    assert t.usage.input_tokens == 50
    assert t.usage.output_tokens == 70
    assert t.model == "MiniMax-M3.1-Flash"


def test_cache_tokens_count_as_input():
    u = Usage(input_tokens=10, output_tokens=5, cache_read_input_tokens=1000)
    # a total that leaves the cache out reads as a nearly-free session
    assert u.billed_input == 1010
    assert u.total == 1015


def test_usage_from_obj_rejects_absence_and_zeroes():
    assert usage_from_obj({}) is None
    assert usage_from_obj(None) is None
    assert usage_from_obj({"usage": {}}) is None
    assert usage_from_obj({"usage": {"input_tokens": 0, "output_tokens": 0}}) is None
    u = usage_from_obj({"usage": {"input_tokens": 3, "service_tier": "standard"}})
    assert u.input_tokens == 3
    assert u.extra["service_tier"] == "standard"


def test_unknown_usage_keys_are_kept_but_not_totalled():
    t = UsageTally()
    t.absorb_json(
        b'{"model":"m","usage":{"input_tokens":1,"output_tokens":2,"server_tool_use":{"web_search_requests":1}}}'
    )
    assert t.usage.total == 3
    assert t.usage.extra["server_tool_use"] == {"web_search_requests": 1}


def test_ledger_appends_one_line_per_request(tmp_path):
    p = tmp_path / "usage.jsonl"
    led = UsageLedger(p)
    led.record(provider="deepseek", model="a", usage=Usage(input_tokens=1, output_tokens=2))
    led.record(provider="deepseek", model="a", usage=Usage(input_tokens=3, output_tokens=4))
    lines = p.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["total"] == 3
    assert json.loads(lines[1])["total"] == 7


def test_ledger_mode_is_private(tmp_path):
    p = tmp_path / "usage.jsonl"
    UsageLedger(p).record(provider="p", model="m", usage=Usage(input_tokens=1))
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"


def test_empty_usage_is_not_written(tmp_path):
    p = tmp_path / "usage.jsonl"
    led = UsageLedger(p)
    led.record(provider="p", model="m", usage=Usage())
    led.record(provider="p", model="m", usage=None)
    assert not p.exists()


def test_unwritable_path_does_not_raise(tmp_path):
    """A full disk must not take down the proxy carrying the conversation."""
    led = UsageLedger(tmp_path / "nope" / "usage.jsonl")
    os.chmod(tmp_path, 0o500)
    try:
        led.record(provider="p", model="m", usage=Usage(input_tokens=1))
    finally:
        os.chmod(tmp_path, 0o700)


def test_corrupt_line_is_skipped(tmp_path):
    p = tmp_path / "usage.jsonl"
    p.write_text('{"ts":"2026-10-24T00:00:00+00:00","total":5}\nnot json\n\n')
    assert len(read_ledger(p)) == 1


def test_aggregate_by_day_and_model(tmp_path):
    p = tmp_path / "usage.jsonl"
    led = UsageLedger(p)
    led.record(provider="deepseek", model="a", usage=Usage(input_tokens=10, output_tokens=1))
    led.record(provider="trae", model="b", usage=Usage(input_tokens=20, output_tokens=2))
    recs = read_ledger(p)
    by_model = aggregate(recs, "model")
    assert set(by_model) == {"a", "b"}
    assert by_model["a"].total == 11
    by_provider = aggregate(recs, "provider")
    assert by_provider["trae"].total == 22
    by_day = aggregate(recs, "day")
    assert sum(b.total for b in by_day.values()) == 33
