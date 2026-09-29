#!/usr/bin/env python3
import json
import unittest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway import (
    anthropic_to_openai_messages,
    anthropic_tools_to_openai,
    drop_tracking_fields,
    openai_tool_calls_to_anthropic,
    route_upstream,
    unwrap_openai_response,
)


class TestGatewayHelpers(unittest.TestCase):
    def test_route_cmdc_default(self):
        name, base, model = route_upstream("deepseek/deepseek-v4.1-flash")
        self.assertEqual(name, "cmdc")
        self.assertIn("commandcode.ai", base)
        self.assertEqual(model, "deepseek/deepseek-v4.1-flash")

    def test_route_cline_alias(self):
        name, base, model = route_upstream("cline-deepseek")
        self.assertEqual(name, "cline")
        self.assertIn("cline.bot", base)
        # Token Plan 必须带 cline-pass/ 前缀，否则走按量钱包会 402
        self.assertEqual(model, "cline-pass/deepseek-v4.1-flash")

    def test_drop_user_and_metadata(self):
        out = drop_tracking_fields(
            {"model": "x", "messages": [], "user": "claude-user", "metadata": {"user_id": "1"}}
        )
        self.assertEqual(out, {"model": "x", "messages": []})
        self.assertNotIn("user", out)
        self.assertNotIn("metadata", out)

    def test_unwrap_nested_choices(self):
        nested = {"success": True, "data": {"choices": [{"message": {"content": "hi"}}], "id": "c1"}}
        out = unwrap_openai_response(nested)
        self.assertIn("choices", out)
        self.assertEqual(out["choices"][0]["message"]["content"], "hi")
        self.assertTrue(out.get("success"))

    def test_unwrap_already_flat(self):
        flat = {"choices": [{"message": {"content": "ok"}}]}
        self.assertEqual(unwrap_openai_response(flat), flat)

    def test_tools_schema_roundtrip(self):
        oa = anthropic_tools_to_openai(
            [{"name": "Bash", "description": "run", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}}]
        )
        self.assertEqual(oa[0]["function"]["name"], "Bash")
        self.assertEqual(oa[0]["function"]["parameters"]["properties"]["command"]["type"], "string")

    def test_tool_use_and_result_messages(self):
        msgs = anthropic_to_openai_messages(
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "id": "call_1", "name": "Bash", "input": {"command": "pwd"}},
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "call_1", "content": "/home"},
                        ],
                    },
                ]
            }
        )
        self.assertEqual(msgs[0]["role"], "assistant")
        self.assertEqual(msgs[0]["tool_calls"][0]["function"]["name"], "Bash")
        self.assertEqual(json.loads(msgs[0]["tool_calls"][0]["function"]["arguments"])["command"], "pwd")
        self.assertEqual(msgs[1]["role"], "tool")
        self.assertEqual(msgs[1]["tool_call_id"], "call_1")
        self.assertEqual(msgs[1]["content"], "/home")

    def test_openai_tool_calls_to_anthropic(self):
        blocks = openai_tool_calls_to_anthropic(
            [{"id": "call_1", "function": {"name": "Read", "arguments": '{"path": "/tmp/a"}'}}]
        )
        self.assertEqual(blocks[0]["type"], "tool_use")
        self.assertEqual(blocks[0]["name"], "Read")
        self.assertEqual(blocks[0]["input"]["path"], "/tmp/a")


if __name__ == "__main__":
    unittest.main()
