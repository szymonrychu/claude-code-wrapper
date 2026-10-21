"""ccw MCP server (D1): one `command` tool over stateless JSON-RPC on the internal API.

`handle_message` is transport-free: it takes one decoded JSON-RPC message and the session's
router and returns the response object, or None for a notification (HTTP 202).
"""

from __future__ import annotations

from typing import Any

from claude_code_wrapper import __version__
from claude_code_wrapper.commands import CommandRejected, params_schema

SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26")
DEFAULT_PROTOCOL = "2025-06-18"
TOOL_NAME = "command"

INSTRUCTIONS = (
    "The `command` tool queues a Claude Code slash command (for example compact or "
    "reload-plugins). It runs when you end your turn, after which you are resumed "
    "automatically. After calling it, end your turn with a one-line status."
)
TOOL_DESCRIPTION = (
    "Queue an allowlisted slash command for this session. The command runs when you end your "
    "turn; you are then resumed automatically to continue your work."
)
DISABLED_TEXT = "ccw commands are disabled for this session"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


def error_response(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _tool_result(text: str, *, is_error: bool) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _call(router: Any, params: Any) -> dict[str, Any] | None:
    """The tools/call result, or None when the tool name is not `command`."""
    if not isinstance(params, dict) or params.get("name") != TOOL_NAME:
        return None
    if not router.commands_cfg.agent:
        return _tool_result(DISABLED_TEXT, is_error=True)
    args = params.get("arguments")
    if not isinstance(args, dict) or not isinstance(args.get("name"), str):
        return _tool_result("arguments must be an object with a string `name`", is_error=True)
    cmd_params = args.get("params")
    if cmd_params is not None and not isinstance(cmd_params, dict):
        return _tool_result("`params` must be an object", is_error=True)
    if args["name"] not in router.commands_cfg.agent:
        allowed = ", ".join(router.commands_cfg.agent)
        return _tool_result(
            f"command {args['name']!r} is not allowed; allowed: {allowed}", is_error=True
        )
    try:
        record = router.request_command(args["name"], cmd_params, origin="agent")
    except CommandRejected as exc:
        return _tool_result(exc.message, is_error=True)
    return _tool_result(
        f"Queued {record.id}. It runs when you end this turn; end your turn now with a "
        "one-line status. You will be resumed automatically.",
        is_error=False,
    )


def handle_message(router: Any, message: Any) -> dict[str, Any] | None:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return error_response(None, INVALID_REQUEST, "expected one JSON-RPC 2.0 message")
    if "id" not in message:
        return None
    msg_id = message["id"]
    method = message.get("method")
    params = message.get("params")
    if method == "initialize":
        asked = params.get("protocolVersion") if isinstance(params, dict) else None
        return _result(
            msg_id,
            {
                "protocolVersion": asked if asked in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "ccw", "version": __version__},
                "instructions": INSTRUCTIONS,
            },
        )
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        allowed = list(router.commands_cfg.agent)
        tools = (
            [
                {
                    "name": TOOL_NAME,
                    "description": TOOL_DESCRIPTION,
                    "inputSchema": params_schema(allowed),
                }
            ]
            if allowed
            else []
        )
        return _result(msg_id, {"tools": tools})
    if method == "tools/call":
        result = _call(router, params)
        if result is None:
            return error_response(msg_id, INVALID_PARAMS, "unknown tool")
        return _result(msg_id, result)
    return error_response(msg_id, METHOD_NOT_FOUND, "Method not found")
