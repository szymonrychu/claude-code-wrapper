from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from claude_code_wrapper.commands import CommandRejected  # noqa: F401
from claude_code_wrapper.config import CommandsConfig
from claude_code_wrapper.mcp_server import handle_message
from claude_code_wrapper.peer_client import PeerError

PREFIXES = [
    "unknown peer",
    "peer unreachable",
    "message too large",
    "inbox full",
    "rate limited",
    "peer rejected the request",
    "invalid arguments",
]


class FakeRouter:
    def __init__(self, agent: list[str] | None = None) -> None:
        self.commands_cfg = CommandsConfig(agent=agent or [])
        self.info = SimpleNamespace(session_id="s1")
        self.calls: list[Any] = []

    def request_command(self, name: str, args: str = "", *, origin: str) -> Any:
        self.calls.append((name, args, origin))
        return SimpleNamespace(id="cmd-1")


class FakePeers:
    def __init__(self) -> None:
        self.listed: list[tuple[str, str | None]] = []
        self.sent: list[tuple[str, str, str, bool]] = []
        self.error: PeerError | None = None
        self.rows = [
            {"address": "b/s2", "instance": "b", "session_id": "s2", "name": "n", "cwd": "/w"}
        ]

    async def list_peers(self, caller_session: str, instance: str | None) -> list[dict]:
        self.listed.append((caller_session, instance))
        return self.rows

    async def send(self, caller_session: str, to: str, message: str, notify_when_idle: bool) -> str:
        if self.error is not None:
            raise self.error
        self.sent.append((caller_session, to, message, notify_when_idle))
        return "msg_abc"


def _msg(method: str, params: Any = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


def _call(name: str, args: Any) -> dict[str, Any]:
    return _msg("tools/call", {"name": name, "arguments": args})


async def _tools(router: FakeRouter, peers: Any) -> list[dict]:
    reply = await handle_message(router, _msg("tools/list"), peers)
    return reply["result"]["tools"]


async def test_tools_listed_only_with_peers() -> None:
    assert await _tools(FakeRouter(), None) == []
    assert [t["name"] for t in await _tools(FakeRouter(), FakePeers())] == [
        "peers",
        "send_message",
    ]
    names = [t["name"] for t in await _tools(FakeRouter(["compact"]), FakePeers())]
    assert names == ["command", "peers", "send_message"]
    names = [t["name"] for t in await _tools(FakeRouter(["compact"]), None)]
    assert names == ["command"]


async def test_schemas() -> None:
    tools = {t["name"]: t for t in await _tools(FakeRouter(), FakePeers())}
    p = tools["peers"]["inputSchema"]
    assert p["properties"] == {
        "instance": {"type": "string", "description": p["properties"]["instance"]["description"]}
    }
    assert p["additionalProperties"] is False
    assert "required" not in p
    s = tools["send_message"]["inputSchema"]
    assert s["required"] == ["to", "message"]
    assert s["properties"]["to"]["type"] == "string"
    assert s["properties"]["message"]["type"] == "string"
    assert s["properties"]["notify_when_idle"]["type"] == "boolean"
    assert s["additionalProperties"] is False
    for t in tools.values():
        if t["name"] == "send_message":
            for prefix in PREFIXES:
                assert prefix in t["description"]


async def test_instructions_mention_peers_only_when_enabled() -> None:
    off = await handle_message(FakeRouter(), _msg("initialize", {}), None)
    on = await handle_message(FakeRouter(), _msg("initialize", {}), FakePeers())
    a, b = off["result"]["instructions"], on["result"]["instructions"]
    assert b.startswith(a) and b != a and "send_message" in b


async def test_peers_tool_lists() -> None:
    peers = FakePeers()
    reply = await handle_message(FakeRouter(), _call("peers", {"instance": "b"}), peers)
    res = reply["result"]
    assert res["isError"] is False
    assert json.loads(res["content"][0]["text"]) == peers.rows
    assert peers.listed == [("s1", "b")]
    await handle_message(FakeRouter(), _call("peers", {}), peers)
    assert peers.listed[-1] == ("s1", None)


@pytest.mark.parametrize("args", [{"instance": 3}, {"x": 1}, "str"])
async def test_peers_invalid_arguments(args: Any) -> None:
    reply = await handle_message(FakeRouter(), _call("peers", args), FakePeers())
    assert reply["result"]["isError"] is True
    assert reply["result"]["content"][0]["text"].startswith("invalid arguments")


async def test_send_message_success() -> None:
    peers = FakePeers()
    reply = await handle_message(
        FakeRouter(), _call("send_message", {"to": "b/s2", "message": "hi"}), peers
    )
    assert reply["result"] == {
        "content": [
            {
                "type": "text",
                "text": "Sent msg_abc to b/s2. A reply, if any, arrives as a new turn.",
            }
        ],
        "isError": False,
    }
    assert peers.sent == [("s1", "b/s2", "hi", False)]
    await handle_message(
        FakeRouter(),
        _call("send_message", {"to": "b/s2", "message": "hi", "notify_when_idle": True}),
        peers,
    )
    assert peers.sent[-1][3] is True


@pytest.mark.parametrize("prefix", PREFIXES)
async def test_send_message_errors(prefix: str) -> None:
    peers = FakePeers()
    peers.error = PeerError("code", f"{prefix}: detail")
    reply = await handle_message(
        FakeRouter(), _call("send_message", {"to": "b/s2", "message": "hi"}), peers
    )
    assert reply["result"]["isError"] is True
    assert reply["result"]["content"][0]["text"] == f"{prefix}: detail"


@pytest.mark.parametrize(
    "args",
    [
        {"to": "b/s2"},
        {"message": "hi"},
        {"to": 1, "message": "hi"},
        {"to": "b/s2", "message": 1},
        {"to": "b/s2", "message": ""},
        {"to": "b/s2", "message": "hi", "notify_when_idle": "yes"},
        {"to": "b/s2", "message": "hi", "extra": 1},
        None,
    ],
)
async def test_send_message_argument_validation(args: Any) -> None:
    peers = FakePeers()
    reply = await handle_message(FakeRouter(), _call("send_message", args), peers)
    assert reply["result"]["isError"] is True
    assert reply["result"]["content"][0]["text"].startswith("invalid arguments")
    assert peers.sent == []


@pytest.mark.parametrize("name", ["peers", "send_message"])
async def test_unknown_tool_without_peers(name: str) -> None:
    reply = await handle_message(FakeRouter(), _call(name, {}), None)
    assert reply["error"]["message"] == "unknown tool"


async def test_command_tool_unchanged() -> None:
    for peers in (None, FakePeers()):
        router = FakeRouter(["compact"])
        reply = await handle_message(router, _call("command", {"name": "compact"}), peers)
        assert reply["result"]["isError"] is False
        assert reply["result"]["content"][0]["text"].startswith("Queued cmd-1.")
        assert router.calls == [("compact", "", "agent")]
