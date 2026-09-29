#!/usr/bin/env python3
"""
Claude Code → OpenAI 协议网关（127.0.0.1:4000）

Claude Code 只说 Anthropic /v1/messages；CommandCode / Cline Pass 只说
OpenAI /chat/completions。本进程做双向翻译，避免把 URL 直接填进 Claude Code。

路由：
  model=deepseek/deepseek-v4.1-flash | cmdc-deepseek  → api.commandcode.ai
  model=cline-deepseek                                 → api.cline.bot
      上游模型必须带 cline-pass/ 前缀，否则走按量钱包而不是 Token Plan 订阅

踩坑对应：
  - 发出前丢掉 user / user_id / metadata（CommandCode 遇未知 user → 400）
  - 非流式若 choices 藏在 data 下则提升到顶层（Cline 非标包一层 → 500）
  - 强制走 /chat/completions，不走 /responses
"""
from __future__ import annotations

import json
import os
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PORT = int(os.environ.get("CCP_GATEWAY_PORT", "4000"))
CMDC_BASE = os.environ.get("CMDC_BASE_URL", "https://api.commandcode.ai/provider/v1").rstrip("/")
CLINE_BASE = os.environ.get("CLINE_BASE_URL", "https://api.cline.bot/api/v1").rstrip("/")
CMDC_MODEL = os.environ.get("CMDC_MODEL", "deepseek/deepseek-v4.1-flash")
CLINE_MODEL = os.environ.get("CLINE_UPSTREAM_MODEL", "cline-pass/deepseek-v4.1-flash")
DROP_KEYS = ("user", "user_id", "metadata")


def _read_env_file(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def get_cmdc_key() -> str:
    env_key = os.environ.get("CMDC_API_KEY", "")
    if env_key:
        return env_key
    auth = Path.home() / ".commandcode" / "auth.json"
    if auth.is_file():
        try:
            data = json.loads(auth.read_text(encoding="utf-8"))
            return str(data.get("apiKey") or "")
        except (OSError, json.JSONDecodeError, TypeError):
            return ""
    return ""


def get_cline_key() -> str:
    env_key = os.environ.get("CLINE_API_KEY", "")
    if env_key:
        return env_key
    for cand in (
        Path(os.environ.get("CCP_SECRETS_ENV", "")),
        Path.home() / ".ccp" / "secrets.env",
    ):
        if cand and cand.is_file():
            key = _read_env_file(cand).get("CLINE_API_KEY", "")
            if key:
                return key
    return ""


def route_upstream(model: str) -> Tuple[str, str, str]:
    """Return (name, api_base, upstream_model)."""
    m = (model or "").strip()
    if m.startswith("cline") or m in ("cline-deepseek",):
        return "cline", CLINE_BASE, CLINE_MODEL
    return "cmdc", CMDC_BASE, CMDC_MODEL if not m or m == "cmdc-deepseek" else m


def drop_tracking_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in payload.items() if k not in DROP_KEYS}
    return out


def unwrap_openai_response(obj: Any) -> Any:
    """Cline 非流式：{success, data: {choices: ...}} → 顶层 choices。"""
    if not isinstance(obj, dict):
        return obj
    if "choices" in obj:
        return obj
    data = obj.get("data")
    if isinstance(data, dict) and "choices" in data:
        merged = dict(data)
        for k, v in obj.items():
            if k != "data" and k not in merged:
                merged[k] = v
        return merged
    return obj


def _system_text(system_val: Any) -> str:
    if not system_val:
        return ""
    if isinstance(system_val, str):
        return system_val
    if isinstance(system_val, list):
        return "".join(b.get("text", "") for b in system_val if isinstance(b, dict))
    return ""


def anthropic_tools_to_openai(tools: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isinstance(tools, list):
        return out
    for t in tools:
        if not isinstance(t, dict):
            continue
        name = t.get("name") or ""
        if not name:
            continue
        schema = t.get("input_schema") or t.get("parameters") or {"type": "object", "properties": {}}
        item: Dict[str, Any] = {
            "type": "function",
            "function": {
                "name": name,
                "description": t.get("description") or "",
                "parameters": schema,
            },
        }
        out.append(item)
    return out


def _block_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(x.get("text", "") for x in content if isinstance(x, dict))
    return "" if content is None else str(content)


def anthropic_to_openai_messages(req_body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Anthropic messages（含 tool_use / tool_result）→ OpenAI messages + tool_calls。"""
    out: List[Dict[str, Any]] = []
    sys_text = _system_text(req_body.get("system"))
    if sys_text:
        out.append({"role": "system", "content": sys_text})
    for msg in req_body.get("messages") or []:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            out.append({"role": role, "content": str(content)})
            continue
        texts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []
        tool_results: List[Dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                texts.append(block.get("text") or "")
            elif btype == "tool_use":
                args = block.get("input") if isinstance(block.get("input"), dict) else {}
                tool_calls.append(
                    {
                        "id": block.get("id") or f"call_{uuid.uuid4().hex[:20]}",
                        "type": "function",
                        "function": {
                            "name": block.get("name") or "",
                            "arguments": json.dumps(args, ensure_ascii=False),
                        },
                    }
                )
            elif btype == "tool_result":
                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id") or "",
                        "content": _block_text(block.get("content")),
                    }
                )
        if role == "assistant":
            entry: Dict[str, Any] = {"role": "assistant", "content": "\n".join(texts) if texts else None}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            if entry["content"] is None and not tool_calls:
                entry["content"] = ""
            out.append(entry)
        else:
            if texts:
                out.append({"role": role, "content": "\n".join(texts)})
            out.extend(tool_results)
    return out


def build_openai_payload(req_body: Dict[str, Any], upstream_model: str, stream: bool) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": upstream_model,
        "messages": anthropic_to_openai_messages(req_body),
        "stream": stream,
        "max_tokens": req_body.get("max_tokens", 4096),
    }
    oa_tools = anthropic_tools_to_openai(req_body.get("tools"))
    if oa_tools:
        payload["tools"] = oa_tools
        choice = req_body.get("tool_choice")
        if isinstance(choice, str) and choice in ("auto", "none", "required", "any"):
            payload["tool_choice"] = "required" if choice == "any" else ("auto" if choice == "auto" else choice)
        elif isinstance(choice, dict) and choice.get("type") == "tool" and choice.get("name"):
            payload["tool_choice"] = {
                "type": "function",
                "function": {"name": choice["name"]},
            }
        else:
            payload["tool_choice"] = "auto"
    if "temperature" in req_body:
        payload["temperature"] = req_body["temperature"]
    return drop_tracking_fields(payload)


def openai_tool_calls_to_anthropic(tool_calls: Any) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    if not isinstance(tool_calls, list):
        return blocks
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments") or "{}"
        try:
            parsed = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except json.JSONDecodeError:
            parsed = {"_raw": raw_args}
        if not isinstance(parsed, dict):
            parsed = {"value": parsed}
        blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id") or f"call_{uuid.uuid4().hex[:20]}",
                "name": fn.get("name") or "",
                "input": parsed,
            }
        )
    return blocks


def openai_choice_to_anthropic_message(choice: Dict[str, Any], msg_id: str, model_name: str) -> Dict[str, Any]:
    message = choice.get("message") or {}
    text = message.get("content") or ""
    content: List[Dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    content.extend(openai_tool_calls_to_anthropic(message.get("tool_calls")))
    stop = "tool_use" if message.get("tool_calls") else "end_turn"
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "content": content or [{"type": "text", "text": ""}],
        "model": model_name,
        "stop_reason": stop,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def _http_json(url: str, headers: Dict[str, str], body: bytes, timeout: int = 120) -> Tuple[int, bytes]:
    req = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.getcode() or 200, resp.read()
    except HTTPError as exc:
        return exc.code, exc.read()
    except URLError as exc:
        return 502, json.dumps({"error": str(exc.reason)}).encode("utf-8")


class ThreadingSimpleServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class GatewayHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        sys_stderr = __import__("sys").stderr
        sys_stderr.write("[ccp-gateway] " + (fmt % args) + "\n")

    def _send_json(self, code: int, obj: Any) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path in ("/", "/health", "/v1/health"):
            self._send_json(
                200,
                {
                    "status": "ok",
                    "provider": "ccp-gateway",
                    "upstreams": ["cmdc", "cline"],
                    "port": PORT,
                },
            )
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        if not (self.path.endswith("/messages") or "/v1/messages" in self.path):
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            req_body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": f"Invalid JSON: {exc}"})
            return

        client_model = str(req_body.get("model") or "")
        name, base, upstream_model = route_upstream(client_model)
        if name == "cmdc":
            key = get_cmdc_key()
        else:
            key = get_cline_key()
        if not key:
            self._send_json(
                401,
                {
                    "error": f"missing {name} credentials",
                    "hint": "cmdc: ~/.commandcode/auth.json  cline: CLINE_API_KEY in ~/.ccp/secrets.env",
                },
            )
            return

        want_stream = bool(req_body.get("stream", True))
        openai_payload = build_openai_payload(req_body, upstream_model, stream=want_stream)
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Cloudflare 1010：urllib 默认 UA 会被拦；curl / 带 UA 则 200
            "User-Agent": "ccp-gateway/1.0",
        }
        target = f"{base}/chat/completions"
        msg_id = f"msg_{uuid.uuid4().hex[:20]}"

        if want_stream:
            self._proxy_stream(target, headers, openai_payload, upstream_model, msg_id)
            return

        code, body = _http_json(target, headers, json.dumps(openai_payload).encode("utf-8"))
        try:
            parsed = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        parsed = unwrap_openai_response(parsed)
        if (
            name == "cline"
            and isinstance(parsed, dict)
            and (code >= 500 or (parsed.get("success") is False and not parsed.get("choices")))
        ):
            code2, body2 = _http_json(target, headers, json.dumps(openai_payload).encode("utf-8"))
            try:
                parsed2 = unwrap_openai_response(json.loads(body2.decode("utf-8")))
                if code2 == 200 and isinstance(parsed2, dict) and parsed2.get("choices"):
                    code, parsed = code2, parsed2
            except json.JSONDecodeError:
                pass
        if code != 200:
            self._send_json(code, parsed)
            return
        choices = parsed.get("choices") or []
        choice = choices[0] if choices else {}
        self._send_json(200, openai_choice_to_anthropic_message(choice, msg_id, upstream_model))

    def _proxy_stream(
        self,
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        model_name: str,
        msg_id: str,
    ) -> None:
        req = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            upstream = urlopen(req, timeout=300)
        except HTTPError as exc:
            err_body = exc.read()
            try:
                parsed = unwrap_openai_response(json.loads(err_body.decode("utf-8")))
            except (json.JSONDecodeError, UnicodeDecodeError):
                parsed = {"error": err_body.decode("utf-8", errors="replace")}
            self._send_json(exc.code, parsed)
            return
        except URLError as exc:
            self._send_json(502, {"error": f"Upstream error: {exc.reason}"})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(event: str, data: Dict[str, Any]) -> None:
            self.wfile.write(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        emit(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "model": model_name,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )
        next_index = 0
        text_index: Optional[int] = None
        tool_index_map: Dict[int, int] = {}
        used_tools = False

        def emit_text_delta(piece: str) -> None:
            nonlocal next_index, text_index
            if text_index is None:
                text_index = next_index
                next_index += 1
                emit(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": text_index,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
            emit(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": text_index,
                    "delta": {"type": "text_delta", "text": piece},
                },
            )

        def emit_tool_delta(tc: Dict[str, Any]) -> None:
            nonlocal next_index, used_tools
            oi = int(tc.get("index") or 0)
            if oi not in tool_index_map:
                used_tools = True
                if not tool_index_map and text_index is not None:
                    emit("content_block_stop", {"type": "content_block_stop", "index": text_index})
                ai = next_index
                next_index += 1
                tool_index_map[oi] = ai
                fn = tc.get("function") or {}
                emit(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": ai,
                        "content_block": {
                            "type": "tool_use",
                            "id": tc.get("id") or f"call_{uuid.uuid4().hex[:16]}",
                            "name": fn.get("name") or "",
                            "input": {},
                        },
                    },
                )
            args = (tc.get("function") or {}).get("arguments")
            if args:
                emit(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": tool_index_map[oi],
                        "delta": {"type": "input_json_delta", "partial_json": args},
                    },
                )

        try:
            while True:
                line = upstream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text.startswith("data:"):
                    continue
                data_str = text[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = unwrap_openai_response(json.loads(data_str))
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    emit_text_delta(content)
                for tc in delta.get("tool_calls") or []:
                    if isinstance(tc, dict):
                        emit_tool_delta(tc)
                if choices[0].get("finish_reason"):
                    break
        finally:
            try:
                upstream.close()
            except OSError:
                pass
        if text_index is not None and not used_tools:
            emit("content_block_stop", {"type": "content_block_stop", "index": text_index})
        for ai in tool_index_map.values():
            emit("content_block_stop", {"type": "content_block_stop", "index": ai})
        if text_index is None and not tool_index_map:
            emit(
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            )
            emit("content_block_stop", {"type": "content_block_stop", "index": 0})
        emit(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": "tool_use" if used_tools else "end_turn",
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": 0},
            },
        )
        emit("message_stop", {"type": "message_stop"})


def run(host: str = "127.0.0.1", port: int = PORT) -> None:
    server = ThreadingSimpleServer((host, port), GatewayHandler)
    print(f"[ccp-gateway] http://{host}:{port}  cmdc={CMDC_BASE}  cline={CLINE_BASE}")
    server.serve_forever()


if __name__ == "__main__":
    run()
