"""ccw MCP server (D1): one `command` tool over stateless JSON-RPC on the internal API.

`handle_message` is transport-free: it takes one decoded JSON-RPC message and the session's
router and returns the response object, or None for a notification (HTTP 202).
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from claude_code_wrapper import __version__
from claude_code_wrapper.commands import MAX_COMMAND_ARGS, CommandRejected
from claude_code_wrapper.config import COMMAND_WILDCARD
from claude_code_wrapper.peer_client import PeerError
from claude_code_wrapper.turns import LANES

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
PEERS_TOOL = "peers"
SEND_TOOL = "send_message"
PEERS_INSTRUCTIONS = (
    " The `peers` tool lists the other ccw sessions you can message, and `send_message` sends "
    "one a message, typed into its running turn or arriving as a new turn. Use them instead of "
    "SendMessage; reply to a "
    "peer only when a reply is needed."
)
PEERS_DESCRIPTION = (
    "List the other ccw sessions this session can message: address (<instance>/<session id>), "
    "instance, session_id, name and cwd. Optionally filter by instance."
)
SEND_DESCRIPTION = (
    "Send a message to another ccw session by address (<instance>/<session id>, from `peers`). "
    "By lane: regular (default) is typed into the peer's running turn, or starts a turn when it "
    "is idle; idle waits until the peer is idle; priority interrupts the peer's running turn. A "
    "reply, if any, arrives here the same way. Set "
    "notify_when_idle to be told when the peer has handled it. Errors start with one of: "
    "unknown peer, peer unreachable, message too large, inbox full, rate limited, "
    "peer rejected the request, invalid arguments."
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


class PeerTools(Protocol):
    async def list_peers(self, caller_session: str, instance: str | None) -> list[dict]: ...

    async def send(
        self, caller_session: str, to: str, message: str, notify_when_idle: bool, lane: str
    ) -> str: ...


def command_schema(allowed: list[str]) -> dict[str, Any]:
    """Flat input schema `{name, args?}` of the `command` tool (no oneOf/anyOf/allOf).

    `name` is an enum of the agent allowlist, or a plain string when it holds `*`.
    """
    name: dict[str, Any] = {
        "type": "string",
        "description": "the slash command to run, without the leading /",
    }
    if COMMAND_WILDCARD not in allowed:
        name["enum"] = list(allowed)
    return {
        "type": "object",
        "properties": {
            "name": name,
            "args": {
                "type": "string",
                "description": "the text typed after the command name; may span lines, at most "
                f"{MAX_COMMAND_ARGS} characters; omit for none",
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    }


def _invalid(detail: str) -> dict[str, Any]:
    return _tool_result(f"invalid arguments: {detail}", is_error=True)


def _peer_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": PEERS_TOOL,
            "description": PEERS_DESCRIPTION,
            "inputSchema": {
                "type": "object",
                "properties": {"instance": {"type": "string", "description": "only this instance"}},
                "additionalProperties": False,
            },
        },
        {
            "name": SEND_TOOL,
            "description": SEND_DESCRIPTION,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "<instance>/<session id>"},
                    "message": {"type": "string", "description": "the message text"},
                    "notify_when_idle": {"type": "boolean"},
                    "lane": {
                        "type": "string",
                        "enum": list(LANES),
                        "description": "regular (default), idle or priority",
                    },
                },
                "required": ["to", "message"],
                "additionalProperties": False,
            },
        },
    ]


async def _call_peers(router: Any, name: str, args: Any, peers: PeerTools) -> dict[str, Any]:
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return _invalid("arguments must be an object")
    allowed = {"instance"} if name == PEERS_TOOL else {"to", "message", "notify_when_idle", "lane"}
    extra = sorted(set(args) - allowed)
    if extra:
        return _invalid(f"unknown arguments {', '.join(extra)}")
    caller = router.info.session_id
    if name == PEERS_TOOL:
        instance = args.get("instance")
        if instance is not None and not isinstance(instance, str):
            return _invalid("`instance` must be a string")
        rows = await peers.list_peers(caller, instance)
        return _tool_result(json.dumps(rows), is_error=False)
    to, message, notify = args.get("to"), args.get("message"), args.get("notify_when_idle", False)
    if not isinstance(to, str) or not isinstance(message, str):
        return _invalid("`to` and `message` must be strings")
    if not message:
        return _invalid("`message` must not be empty")
    if not isinstance(notify, bool):
        return _invalid("`notify_when_idle` must be a boolean")
    lane = args.get("lane", "regular")
    if lane not in LANES:
        return _invalid(f"`lane` must be one of {', '.join(LANES)}")
    try:
        msg_id = await peers.send(caller, to, message, notify, lane)
    except PeerError as exc:
        return _tool_result(exc.message, is_error=True)
    return _tool_result(
        f"Sent {msg_id} to {to}. A reply, if any, arrives here as a message.", is_error=False
    )


def _call(router: Any, params: Any) -> dict[str, Any] | None:
    """The tools/call result, or None when the tool name is not `command`."""
    if not isinstance(params, dict) or params.get("name") != TOOL_NAME:
        return None
    if not router.commands_cfg.agent:
        return _tool_result(DISABLED_TEXT, is_error=True)
    args = params.get("arguments")
    if not isinstance(args, dict) or not isinstance(args.get("name"), str):
        return _tool_result("arguments must be an object with a string `name`", is_error=True)
    extra = sorted(set(args) - {"name", "args"})
    if extra:
        return _tool_result(
            f"unknown arguments {', '.join(extra)}; allowed: name, args", is_error=True
        )
    cmd_args = args.get("args", "")
    if not isinstance(cmd_args, str):
        return _tool_result("`args` must be a string", is_error=True)
    try:
        record = router.request_command(args["name"], cmd_args, origin="agent")
    except CommandRejected as exc:
        return _tool_result(exc.message, is_error=True)
    return _tool_result(
        f"Queued {record.id}. It runs when you end this turn; end your turn now with a "
        "one-line status. You will be resumed automatically.",
        is_error=False,
    )


async def handle_message(
    router: Any, message: Any, peers: PeerTools | None = None
) -> dict[str, Any] | None:
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
                "instructions": INSTRUCTIONS + (PEERS_INSTRUCTIONS if peers else ""),
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
                    "inputSchema": command_schema(allowed),
                }
            ]
            if allowed
            else []
        )
        if peers is not None:
            tools += _peer_tools()
        return _result(msg_id, {"tools": tools})
    if method == "tools/call":
        name = params.get("name") if isinstance(params, dict) else None
        if peers is not None and name in (PEERS_TOOL, SEND_TOOL):
            return _result(msg_id, await _call_peers(router, name, params.get("arguments"), peers))
        result = _call(router, params)
        if result is None:
            return error_response(msg_id, INVALID_PARAMS, "unknown tool")
        return _result(msg_id, result)
    return error_response(msg_id, METHOD_NOT_FOUND, "Method not found")
