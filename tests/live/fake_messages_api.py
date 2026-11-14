"""Stdlib fake of the Anthropic Messages API for the live-claude job (tests/live).

Real Claude Code talks to it through ANTHROPIC_BASE_URL with a dummy key. The reply is
scripted by markers in the conversation's newest user text:

- `LIVE-CASE:<name>` names the case;
- `LIVE-TOOL:<json>` (optional, compact JSON `{"name": ..., "input": {...}}`) is the tool call
  to answer with.

First request of a case (tools offered, no tool_result yet): a `tool_use` of LIVE-TOOL, or the
text `done <name>` when the case has no tool. After the tool_result: the text `done <name>`.
Requests without a case marker (title or topic side requests) get the text `ok`.

Every request is appended to the JSON-lines log (argv[2]) as `{path, case, reply,
tool_results, body}`; the tests read the tool_results from it. Serves until killed.

usage: fake_messages_api.py <port> <log.jsonl> [<bind host>]
"""

from __future__ import annotations

import json
import re
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CASE_RE = re.compile(r"LIVE-CASE:([A-Za-z0-9_.-]+)")
TOOL_RE = re.compile(r"LIVE-TOOL:(\{.*\})")
LOCK = threading.Lock()


def _texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    out = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            out.append(str(block.get("text", "")))
    return out


def _tool_results(content: Any) -> list[dict[str, Any]]:
    out = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            inner = block.get("content")
            text = inner if isinstance(inner, str) else "\n".join(_texts(inner))
            out.append(
                {
                    "tool_use_id": block.get("tool_use_id"),
                    "is_error": bool(block.get("is_error")),
                    "content": text,
                }
            )
    return out


def _case_and_tool(messages: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any] | None]:
    """The newest user text that carries a case marker: its case name and LIVE-TOOL call."""
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        for text in reversed(_texts(msg.get("content"))):
            case = CASE_RE.search(text)
            if not case:
                continue
            tool = TOOL_RE.search(text)
            return case.group(1), json.loads(tool.group(1)) if tool else None
    return None, None


def decide(body: dict[str, Any]) -> tuple[str | None, str, Any, list[dict[str, Any]]]:
    """(case, kind, value, tool_results of the newest message): kind is `tool` or `text`."""
    messages = body.get("messages") or []
    last = messages[-1] if messages else {}
    results = _tool_results(last.get("content")) if last.get("role") == "user" else []
    case, tool = _case_and_tool(messages)
    if case is None:
        return None, "text", "ok", results
    offered = {t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)}
    if results or tool is None or tool.get("name") not in offered:
        return case, "text", f"done {case}", results
    return case, "tool", tool, results


def _sse(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    log_path = ""

    def log_message(self, *args: Any) -> None:
        pass

    def _json(self, obj: Any, code: int = 200) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        self._json({"data": [], "has_more": False})

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        path = self.path
        is_messages = path.startswith("/v1/messages") and "count_tokens" not in path
        case, kind, value, results = decide(body) if is_messages else (None, "text", "ok", [])
        with LOCK, open(self.log_path, "a") as f:
            record = {
                "path": path,
                "case": case,
                "reply": [kind, value],
                "tool_results": results,
                "body": body,
            }
            f.write(json.dumps(record) + "\n")
        if "count_tokens" in path:
            return self._json({"input_tokens": 10})
        if not path.startswith("/v1/messages"):
            return self._json({})
        self._reply(body, kind, value)

    def _reply(self, body: dict[str, Any], kind: str, value: Any) -> None:
        model = body.get("model", "claude-sonnet-4-5")
        mid = "msg_" + uuid.uuid4().hex[:20]
        if kind == "tool":
            tool_id = "toolu_" + uuid.uuid4().hex[:20]
            block = {"type": "tool_use", "id": tool_id, "name": value["name"], "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(value["input"])}
            full = dict(block, input=value["input"])
            stop = "tool_use"
        else:
            block = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": value}
            full = {"type": "text", "text": value}
            stop = "end_turn"
        usage = {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        message = {
            "id": mid,
            "type": "message",
            "role": "assistant",
            "model": model,
            "stop_sequence": None,
            "usage": usage,
        }
        if not body.get("stream"):
            return self._json({**message, "content": [full], "stop_reason": stop})
        payload = _sse(
            [
                (
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {**message, "content": [], "stop_reason": None},
                    },
                ),
                (
                    "content_block_start",
                    {"type": "content_block_start", "index": 0, "content_block": block},
                ),
                (
                    "content_block_delta",
                    {"type": "content_block_delta", "index": 0, "delta": delta},
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": stop, "stop_sequence": None},
                        "usage": {"output_tokens": 5},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            ]
        )
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main(argv: list[str]) -> None:
    port, log_path = int(argv[1]), argv[2]
    host = argv[3] if len(argv) > 3 else "127.0.0.1"
    Handler.log_path = log_path
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main(sys.argv)
