"""Peer registration across the session lifecycle: two managers reaching each other."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from conftest import World, jsonl
from fastapi import FastAPI

from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.config import Config
from claude_code_wrapper.manager import (
    CreateSessionRequest,
    CreateVetoed,
    InvalidState,
    SessionManager,
)

TOKEN = "peer-token-0123456789abcdef"
PEERS_TOOL = {
    "match": "^list",
    "steps": [{"mcp": {"server": "ccw", "tool": "peers", "arguments": {}}}, {"text": "listed"}],
}


class Mesh(httpx.AsyncBaseTransport):
    """Routes http://<instance> requests to that instance's public app, in process."""

    def __init__(self) -> None:
        self.apps: dict[str, FastAPI] = {}
        self.requests: list[httpx.Request] = []
        self._clients: dict[str, httpx.AsyncClient] = {}

    def add(self, host: str, app: FastAPI) -> None:
        self.apps[host] = app
        self._clients[host] = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://" + host
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        r = await self._clients[request.url.host].request(
            request.method,
            request.url.raw_path.decode(),
            headers=[(k, v) for k, v in request.headers.items() if k != "host"],
            content=request.content,
        )
        return httpx.Response(r.status_code, headers=r.headers, content=r.content)

    async def aclose(self) -> None:
        for c in self._clients.values():
            await c.aclose()


def peers_cfg(
    world: World, name: str, others: list[str], scenario: dict | None = None, **sections
) -> Config:
    return world.cfg(
        scenario,
        server={"allow_anonymous": True},
        peers={
            "enabled": True,
            "name": name,
            "advertise_url": f"http://{name}",
            "token": TOKEN,
            "static": [f"http://{o}" for o in others],
            "heartbeat_seconds": 30,
        },
        **sections,
    )


async def spawn(
    world: World, mesh: Mesh, name: str, others: list[str], scenario: dict | None = None, **sec
) -> SessionManager:
    m = world.manager(peers_cfg(world, name, others, scenario, **sec))
    mesh.add(name, create_app(m, m.cfg))
    await m.start_peers(8000, transport=mesh)
    return m


@pytest.fixture
async def mesh():
    t = Mesh()
    yield t
    await t.aclose()


async def test_enabled_session_wiring(world: World, mesh: Mesh) -> None:
    a = await spawn(world, mesh, "a", [])
    sid = (await a.create(CreateSessionRequest()))["session_id"]
    env = jsonl(world.log)[-1]["env"]
    assert env["CLAUDE_CODE_HARBOR_KITE"] == "0"
    cfgdir = world.session_dir(sid) / "config"
    settings = json.loads((cfgdir / "settings.json").read_text())
    assert {"SendMessage", "ListAgents"} <= set(settings["permissions"]["deny"])
    assert {"mcp__ccw__peers", "mcp__ccw__send_message"} <= set(settings["permissions"]["allow"])
    assert "ccw" in json.loads((cfgdir / ".claude.json").read_text())["mcpServers"]
    assert "mcp__ccw__peers" in (cfgdir / "CLAUDE.md").read_text()


async def test_peers_tool_lists_other_manager(world: World, mesh: Mesh) -> None:
    sc = {"turns": [PEERS_TOOL]}
    a = await spawn(world, mesh, "a", ["b"], sc)
    b = await spawn(world, mesh, "b", ["a"], sc)
    sa = (await a.create(CreateSessionRequest()))["session_id"]
    sb = (await b.create(CreateSessionRequest()))["session_id"]
    await world.until(lambda: any(r["session_id"] == sb for r in listing(a)), what="a sees b")
    await a.router(sa).submit_turn("list")
    await world.wait_turn(a, sa)
    out = jsonl(world.session_dir(sa) / "config" / "fake-mcp.jsonl")[-1]
    text = json.dumps(out["responses"][-1])
    assert f"b/{sb}" in text
    assert f"a/{sa}" not in text


def listing(m: SessionManager) -> list[dict]:
    assert m.peers is not None
    return m.peers.table.sessions()


async def test_destroy_unregisters(world: World, mesh: Mesh) -> None:
    a = await spawn(world, mesh, "a", ["b"])
    b = await spawn(world, mesh, "b", ["a"])
    sb = (await b.create(CreateSessionRequest()))["session_id"]
    await world.until(lambda: any(r["session_id"] == sb for r in listing(a)), what="a sees b")
    await b.destroy(sb)
    await world.until(lambda: not any(r["session_id"] == sb for r in listing(a)), what="gone")


async def test_vetoed_and_early_creates(world: World, mesh: Mesh) -> None:
    deny = {
        "type": "command",
        "run": 'echo \'{"decision": "deny", "reason": "no"}\'',
        "blocking": True,
    }
    a = await spawn(world, mesh, "a", [], hooks={"onCreate": [deny]})
    with pytest.raises(CreateVetoed):
        await a.create(CreateSessionRequest())
    assert a.peers is not None
    assert a.peers._sessions == {}
    m = world.manager(peers_cfg(world, "a", []))
    with pytest.raises(InvalidState, match="peers joining"):
        await m.create(CreateSessionRequest())
    with pytest.raises(InvalidState, match="peers joining"):
        await m.resume("ses_0123456789ab")


async def test_disabled_changes_nothing(world: World, mesh: Mesh, tmp_path: Path) -> None:
    rec = tmp_path / "rec.txt"
    hook = {"type": "command", "run": f"cat >> '{rec}'"}
    m = world.manager(
        world.cfg(hooks={e: [hook] for e in ("preJoin", "postJoin", "preLeave", "postLeave")})
    )
    assert m.peers is None
    await m.start_peers(8000, transport=mesh)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    await m.leave_peers()
    await m.stop_peers(1)
    env = jsonl(world.log)[-1]["env"]
    assert env["CLAUDE_CODE_HARBOR_KITE"] != "0"
    cfgdir = world.session_dir(sid) / "config"
    assert "mcpServers" not in json.loads((cfgdir / ".claude.json").read_text()) or "ccw" not in (
        json.loads((cfgdir / ".claude.json").read_text()).get("mcpServers", {})
    )
    assert "SendMessage" not in (cfgdir / "settings.json").read_text()
    assert not rec.exists()
    assert mesh.requests == []
