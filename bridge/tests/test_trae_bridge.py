"""Guards for the Trae adapter (bridge/trae_bridge.py).

Every rule here exists because violating it is silent or unhelpful:

* a tool's ``parameters`` sent as an object gets HTTP 500 with the bare body
  ``Internal Server Error`` — the most expensive detail in the protocol to
  rediscover;
* dropping or double-flattening a user turn rewrites the prompt (this shipped
  once, as every prompt became the literal string "typetext");
* the event kinds are Trae's own names (``token_usage``, not ``usage``);
* SSE without chunked framing never terminates for a strict HTTP/1.1 client,
  which is what Claude Code is — the turn hangs instead of returning.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))

import trae_bridge as tb  # noqa: E402


# ── message translation ────────────────────────────────────────────────────

def test_plain_string_user_message_survives_intact():
    """A string message must not be iterated per character.

    The bug this guards shipped real: the content was wrapped as a
    {"type":"text"} dict and re-flattened, so the model received the *keys*
    of that dict — every prompt arrived as "typetext".
    """
    out = tb.anthropic_to_trae([{"role": "user", "content": "What is 17*23?"}], None)
    assert out == [{"role": "user",
                    "content": [{"type": "text", "text": "What is 17*23?"}]}]


def test_content_block_list_coalesces_into_one_user_turn():
    """Several blocks in one Anthropic turn are one Trae turn.

    Splitting them would send Trae a conversation with more user messages than
    the client has — the prompt shape must survive the translation.
    """
    out = tb.anthropic_to_trae(
        [{"role": "user", "content": [{"type": "text", "text": "hi"},
                                      {"type": "text", "text": " there"}]}], None)
    assert len(out) == 1
    assert out[0]["content"] == [{"type": "text", "text": "hi"},
                                 {"type": "text", "text": " there"}]


def test_system_prompt_leads_the_conversation():
    out = tb.anthropic_to_trae([{"role": "user", "content": "hi"}], "Be terse.")
    assert out[0] == {"role": "system",
                      "content": [{"type": "text", "text": "Be terse."}]}
    assert out[1]["role"] == "user"


def test_empty_system_prompt_is_dropped():
    out = tb.anthropic_to_trae([{"role": "user", "content": "hi"}], "")
    assert [m["role"] for m in out] == ["user"]


def test_tool_use_and_result_round_trip_into_traes_shape():
    out = tb.anthropic_to_trae([
        {"role": "assistant", "content": [
            {"type": "text", "text": " "},
            {"type": "tool_use", "id": "t1", "name": "get_weather",
             "input": {"city": "Beijing"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "22C"}]},
    ], None)
    call = out[0]["tool_calls"][0]
    # Trae nests the call under `function_call` (singular), not `function`.
    assert call["function_call"]["name"] == "get_weather"
    assert json.loads(call["function_call"]["arguments"]) == {"city": "Beijing"}
    assert out[1]["role"] == "tool"
    assert out[1]["tool_call_id"] == "t1"


def test_tool_result_inside_a_mixed_user_turn_is_split_out():
    out = tb.anthropic_to_trae([
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "42"},
            {"type": "text", "text": "and then?"}]},
    ], None)
    assert out[0]["role"] == "tool"
    assert out[1]["role"] == "user" and out[1]["content"][0]["text"] == "and then?"


def test_image_and_text_coalesce_into_one_user_turn():
    """Trae emits a pasted image as image_url *inside* the user turn.

    Splitting it into two user messages would change the conversation shape
    Trae itself produces.
    """
    out = tb.anthropic_to_trae([{"role": "user", "content": [
        {"type": "text", "text": "what is this?"},
        {"type": "image", "source": {"type": "base64",
                                     "media_type": "image/png",
                                     "data": "iVBORw0KGgo="}}]}], None)
    assert len(out) == 1
    assert out[0]["role"] == "user"
    assert out[0]["content"][0] == {"type": "text", "text": "what is this?"}
    assert out[0]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}


def test_url_image_source_is_passed_through():
    out = tb.anthropic_to_trae([{"role": "user", "content": [
        {"type": "image", "source": {"type": "url",
                                     "url": "https://x/y.png"}}]}], None)
    assert out[0]["content"][0]["image_url"]["url"] == "https://x/y.png"


def test_untranslatable_image_says_so_instead_of_vanishing():
    """A dropped image would silently answer a question about nothing."""
    out = tb.anthropic_to_trae([{"role": "user", "content": [
        {"type": "text", "text": "look:"},
        {"type": "image", "source": {"type": "file", "file_id": "f1"}}]}], None)
    texts = [p["text"] for p in out[0]["content"] if p["type"] == "text"]
    assert any("omitted" in t for t in texts)


def test_tool_result_image_rides_on_its_own_user_turn():
    """An image cannot go on a `role: tool` message.

    Claude Code's Read on a .png produces exactly this shape: text plus an
    image inside one tool_result.
    """
    out = tb.anthropic_to_trae([
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Read",
             "input": {"file_path": "p.png"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": [
                {"type": "text", "text": "image/png file read: p.png"},
                {"type": "image", "source": {"type": "base64",
                                             "media_type": "image/png",
                                             "data": "AAAA"}}]}]}], None)
    assert [m["role"] for m in out] == ["assistant", "tool", "user"]
    # The tool message carries the text only — no "[image]" placeholder, since
    # the pixels travel on the next turn.
    assert out[1]["content"] == [{"type": "text",
                                  "text": "image/png file read: p.png"}]
    assert out[2]["content"][0]["image_url"]["url"] == "data:image/png;base64,AAAA"


def test_text_of_notes_images_only_where_requested():
    content = [{"type": "text", "text": "a"},
               {"type": "image", "source": {"type": "url", "url": "u"}}]
    assert tb._text_of(content) == "a[image]"
    assert tb._text_of(content, images_as_placeholder=False) == "a"


# ── tool translation ───────────────────────────────────────────────────────

def test_tool_parameters_are_a_json_string():
    """The one detail that fails with an otherwise-correct request."""
    tools = tb.anthropic_tools_to_trae([{
        "name": "get_weather", "description": "Get weather",
        "input_schema": {"type": "object",
                         "properties": {"city": {"type": "string"}},
                         "required": ["city"]}}])
    params = tools[0]["function"]["parameters"]
    assert isinstance(params, str), "Trae answers HTTP 500 for an object here"
    assert json.loads(params) == {
        "type": "object", "properties": {"city": {"type": "string"}},
        "required": ["city"]}


def test_tool_without_schema_still_gets_valid_json():
    tools = tb.anthropic_tools_to_trae([{"name": "ping"}])
    assert json.loads(tools[0]["function"]["parameters"]) == {
        "type": "object", "properties": {}}


def test_nameless_tools_are_dropped_rather_than_sent_broken():
    assert tb.anthropic_tools_to_trae([{"description": "no name"}]) == []


# ── response translation ───────────────────────────────────────────────────

def test_stop_reason_maps_trae_finish_reasons():
    assert tb._stop_reason("stop", False) == "end_turn"
    assert tb._stop_reason("length", False) == "max_tokens"
    # A tool call wins over Trae's own finish_reason: it is frequently empty.
    assert tb._stop_reason("", True) == "tool_use"


def test_stop_reason_is_end_turn_without_tools():
    assert tb._stop_reason("", False) == "end_turn"


# ── stream consumption ─────────────────────────────────────────────────────

class _Queue:
    """A queue stub that raises if anyone asks for more than was queued.

    That is exactly how the terminator bug showed up: the reader fetched one
    event past `eof` and blocked forever on an empty queue.

    ``take`` mirrors ``_messages``: the caller pulls the first event to decide
    the HTTP status, and hands that already-consumed event to ``_drain``.
    """
    def __init__(self, items):
        self.items = list(items)

    def get(self):
        if not self.items:
            raise AssertionError("read past the end of the stream")
        return self.items.pop(0)

    def take(self):
        """Peek like the handler does, then return (queue, first_event)."""
        return self, self.get()


def test_drain_parses_every_event_then_stops_at_eof():
    events = [("metadata", json.dumps({"model": "glm-5.3"})),
              ("output", {"response": "hi"}),
              ("token_usage", json.dumps({"prompt_tokens": 7,
                                          "completion_tokens": 2})),
              ("done", json.dumps({"finish_reason": "stop"})),
              ("eof", None)]
    q, first = _Queue(events).take()
    got = list(tb.Handler._drain(q, first))
    assert [k for k, _ in got] == ["metadata", "output", "token_usage", "done"]


def test_drain_stops_on_error_without_reading_further():
    events = [("error", "boom"), ("output", {"response": "unreachable"})]
    q, first = _Queue(events).take()
    assert list(tb.Handler._drain(q, first)) == []


def test_drain_tolerates_unparseable_payloads():
    events = [("token_usage", "not json"), ("output", {"response": "ok"}),
              ("eof", None)]
    q, first = _Queue(events).take()
    got = list(tb.Handler._drain(q, first))
    assert [k for k, _ in got] == ["output"]


def test_token_usage_event_name_is_the_one_trae_sends():
    """`usage` never matches; the producer emits `token_usage`.

    With the typo, usage silently stayed at zero for every response.
    """
    src = (BRIDGE / "trae_bridge.py").read_text()
    assert 'elif event in ("done", "token_usage", "metadata")' in src
    assert src.count('if kind == "token_usage":') == 2
    assert 'if kind == "usage":' not in src


# ── SSE framing ────────────────────────────────────────────────────────────

def test_stream_response_declares_chunked_framing():
    """Claude Code's client hangs without it.

    `Connection: keep-alive` with neither Content-Length nor chunked encoding
    makes a strict HTTP/1.1 reader wait for an EOF that never arrives: the
    events show up but the turn never completes.
    """
    src = (BRIDGE / "trae_bridge.py").read_text()
    assert 'self.send_header("Transfer-Encoding", "chunked")' in src
    assert 'self.send_header("Connection", "keep-alive")' not in src


def test_sse_frames_each_event_as_its_own_chunk():
    h = _FakeHandler()
    h._chunked = True
    h._sse("message_stop", {"type": "message_stop"})
    # "<hex length>\r\n" + payload + "\r\n", with the length matching the
    # payload exactly — a wrong count desynchronises the whole stream.
    body = h.written.split(b"\r\n", 1)[1]
    assert h.written.startswith(b"34\r\n")          # 52 bytes = the event
    assert body == b'event: message_stop\ndata: {"type": "message_stop"}\n\n\r\n'
    assert len(body) - 2 == 52


def test_sse_without_chunked_framing_writes_the_bare_event():
    h = _FakeHandler()
    h._sse("message_stop", {"type": "message_stop"})
    assert h.written.startswith(b"event: message_stop")


def test_terminating_chunk_is_written_only_when_chunked():
    plain = _FakeHandler()
    plain._end_chunked()
    assert plain.written == b""

    chunked = _FakeHandler()
    chunked._chunked = True
    chunked._end_chunked()
    assert chunked.written == b"0\r\n\r\n"


class _FakeHandler(tb.Handler):
    """Exercise the framing helpers without a socket."""

    class _W:
        def __init__(self):
            self.data = b""

        def write(self, b):
            self.data += b

        def flush(self):
            pass

    def __init__(self):
        self.wfile = self._W()
        self._chunked = False

    @property
    def written(self):
        return self.wfile.data

    def log_message(self, *a):
        pass


# ── catalogue ──────────────────────────────────────────────────────────────

def test_placeholder_rows_are_not_offered_as_models():
    """`summary` and `custom_model_placeholder` are internal rows.

    Both carry a model_name, so a model_name-only filter lets them through and
    they end up in the registry as if they were models to pick from.
    """
    payload = {"config_info_list": [
        {"config_name": "glm-5.3",
         "model_detail_list": [{"model_name": "glm-5.3__dev",
                                "prompt_max_tokens": 168000,
                                "max_tokens": 32000}],
         "display_config": {"display_name": "GLM-5.3", "multimodal": False}},
        {"config_name": "summary",
         "model_detail_list": [{"model_name": "Doubao_1_6",
                                "prompt_max_tokens": 40000,
                                "max_tokens": 4096}],
         "display_config": {}},
        {"config_name": "custom_model_placeholder",
         "model_detail_list": [{"model_name": "custom_model_placeholder__v2",
                                "prompt_max_tokens": 30000,
                                "max_tokens": 8192}],
         "display_config": {}},
        {"config_name": "retired-model",
         "model_detail_list": [{}],
         "display_config": {}},
    ]}
    cat = tb._catalogue_from(payload)
    assert [e["config_name"] for e in cat] == ["glm-5.3"]
    assert cat[0]["ctx"] == 168000 and cat[0]["max_output"] == 32000


def test_catalogue_defaults_are_used_when_the_row_is_sparse():
    payload = {"config_info_list": [
        {"config_name": "x", "model_detail_list": [{"model_name": "x__v2"}],
         "display_config": {}}]}
    assert tb._catalogue_from(payload)[0] == {
        "config_name": "x", "model_name": "x__v2", "display_name": "x",
        "ctx": 128000, "max_output": 8192, "vision": False}