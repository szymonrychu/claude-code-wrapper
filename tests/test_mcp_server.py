from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from claude_code_wrapper import __version__
from claude_code_wrapper.commands import CommandRejected, params_schema
from claude_code_wrapper.config import CommandsConfig
from claude_code_wrapper.internal_api import create_internal_app
from claude_code_wrapper.manager import InvalidState

TOKEN = "tok-mcp"
H = {"X-CCW-Session": "s1", "X-CCW-Hook-Token": TOKEN}


class FakeRouter:
    def __init__(self, agent: list[str]) -> None:
        self.commands_cfg = CommandsConfig(agent=agent)
        self.calls: list[tuple[str, Any, str]] = []
        self.reject: CommandRejected | None = None

    def request_command(self, name: str, params: Any = None, *, origin: str) -> Any:
        if self.reject is not None:
            raise self.reject
        self.calls.append((name, params, origin))
        return SimpleNamespace(id="cmd-1")


class FakeManager:
    def __init__(self, agent: list[str]) -> None:
        self.r = FakeRouter(agent)

    def hook_token_matches(self, sid: str, token: str | None) -> bool:
        return sid == "s1" and token == TOKEN

    def router(self, sid: str) -> FakeRouter:
        if sid != "s1":
            raise InvalidState(sid, "stopped")
        return self.r


def client(mgr: FakeManager, host: str = "127.0.0.1") -> httpx.AsyncClient:
    app = create_internal_app(mgr)  # type: ignore[arg-type]
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(host, 1)), base_url="http://t"
    )


def rpc(method: str, params: Any = None, id: Any = 1) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if id is not None:
        msg["id"] = id
    if params is not None:
        msg["params"] = params
    return msg


@pytest.fixture
def mgr() -> FakeManager:
    return FakeManager(["compact", "reload-plugins"])


async def test_mcp_requires_session_token(mgr: FakeManager) -> None:
    async with client(mgr) as c:
        assert (await c.post("/internal/mcp", json=rpc("ping"))).status_code == 403
        bad = {**H, "X-CCW-Hook-Token": "x"}
        assert (await c.post("/internal/mcp", json=rpc("ping"), headers=bad)).status_code == 403
        gone = {**H, "X-CCW-Session": "zz"}
        assert (await c.post("/internal/mcp", json=rpc("ping"), headers=gone)).status_code == 403
    async with client(mgr, host="10.0.0.1") as c:
        assert (await c.post("/internal/mcp", json=rpc("ping"), headers=H)).status_code == 403


async def test_get_and_delete_return_405(mgr: FakeManager) -> None:
    async with client(mgr) as c:
        assert (await c.get("/internal/mcp", headers=H)).status_code == 405
        assert (await c.delete("/internal/mcp", headers=H)).status_code == 405


async def test_unknown_methods_and_bad_messages(mgr: FakeManager) -> None:
    async with client(mgr) as c:
        r = await c.post("/internal/mcp", content=b"{nope", headers=H)
        assert r.json()["error"]["code"] == -32700
        r = await c.post("/internal/mcp", json=[rpc("ping")], headers=H)
        assert r.json()["error"]["code"] == -32600
        r = await c.post("/internal/mcp", json="x", headers=H)
        assert r.json()["error"]["code"] == -32600
        for m in ("server/discover", "resources/list", "nope"):
            r = await c.post("/internal/mcp", json=rpc(m), headers=H)
            assert r.json()["error"]["code"] == -32601


@pytest.mark.parametrize(
    ("asked", "echoed"),
    [("2025-11-25", "2025-11-25"), ("2025-03-26", "2025-03-26"), ("1999-01-01", "2025-06-18")],
)
async def test_initialize_returns_instructions(mgr: FakeManager, asked: str, echoed: str) -> None:
    async with client(mgr) as c:
        r = await c.post(
            "/internal/mcp", json=rpc("initialize", {"protocolVersion": asked}), headers=H
        )
    assert r.headers["content-type"].startswith("application/json")
    res = r.json()["result"]
    assert res["protocolVersion"] == echoed
    assert res["capabilities"] == {"tools": {"listChanged": False}}
    assert res["serverInfo"] == {"name": "ccw", "version": __version__}
    assert "end" in res["instructions"] and "resumed" in res["instructions"]
    assert r.json()["id"] == 1


async def test_notification_returns_202_and_ping(mgr: FakeManager) -> None:
    async with client(mgr) as c:
        r = await c.post("/internal/mcp", json=rpc("notifications/initialized", id=None), headers=H)
        assert r.status_code == 202 and r.content == b""
        r = await c.post("/internal/mcp", json=rpc("ping"), headers=H)
    assert r.json()["result"] == {}


async def test_tools_list_enum_from_agent_allowlist(mgr: FakeManager) -> None:
    async with client(mgr) as c:
        r = await c.post("/internal/mcp", json=rpc("tools/list"), headers=H)
    tools = r.json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["command"]
    assert tools[0]["description"]
    assert tools[0]["inputSchema"] == params_schema(["compact", "reload-plugins"])


async def call(c: httpx.AsyncClient, name: str, args: Any) -> dict[str, Any]:
    r = await c.post(
        "/internal/mcp", json=rpc("tools/call", {"name": name, "arguments": args}), headers=H
    )
    return r.json()


async def test_tools_call_queues_command(mgr: FakeManager) -> None:
    async with client(mgr) as c:
        body = await call(c, "command", {"name": "compact", "params": {"instructions": "x"}})
    res = body["result"]
    assert res["isError"] is False
    assert res["content"] == [
        {
            "type": "text",
            "text": "Queued cmd-1. It runs when you end this turn; end your turn now with a "
            "one-line status. You will be resumed automatically.",
        }
    ]
    assert mgr.r.calls == [("compact", {"instructions": "x"}, "agent")]
    async with client(mgr) as c:
        body = await call(c, "other", {})
    assert body["error"]["code"] == -32602


async def test_tools_call_rejections_are_tool_errors(mgr: FakeManager) -> None:
    async with client(mgr) as c:
        for args in (None, [], {}, {"name": 3}, {"name": "compact", "params": 5}):
            body = await call(c, "command", args)
            assert body["result"]["isError"] is True, args
        assert mgr.r.calls == []
        for code, msg, name in (
            ("unknown_params", "unknown params; allowed: instructions", "compact"),
            ("not_allowed", "/cost is not allowed for agent; allowed: compact", "cost"),
            ("queue_full", "queue is full", "compact"),
        ):
            mgr.r.reject = CommandRejected(code, msg)
            body = await call(c, "command", {"name": name})
            assert body["result"]["isError"] is True
            assert body["result"]["content"][0]["text"] == msg
    assert mgr.r.calls == []


async def test_disabled_session_has_no_tools() -> None:
    m = FakeManager([])
    async with client(m) as c:
        r = await c.post("/internal/mcp", json=rpc("tools/list"), headers=H)
        assert r.json()["result"]["tools"] == []
        body = await call(c, "command", {"name": "compact"})
    assert body["result"]["isError"] is True
    assert body["result"]["content"][0]["text"] == "ccw commands are disabled for this session"
    assert m.r.calls == []


async def test_extra_argument_keys_are_an_error_and_queue_nothing(mgr: FakeManager) -> None:
    async with client(mgr) as c:
        r = await c.post(
            "/internal/mcp",
            json=rpc(
                "tools/call",
                {"name": "command", "arguments": {"name": "compact", "force": True}},
            ),
            headers=H,
        )
    res = r.json()["result"]
    assert res["isError"] is True
    assert "name" in res["content"][0]["text"] and "params" in res["content"][0]["text"]
    assert mgr.r.calls == []
