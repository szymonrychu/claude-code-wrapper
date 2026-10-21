from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest

from claude_code_wrapper.internal_api import create_internal_app
from claude_code_wrapper.manager import InvalidState

TOKEN = "tok-secret-123"


class FakeRouter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.gate: asyncio.Event | None = None

    async def handle_claude_hook(self, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((event, payload))
        if self.gate is not None:
            await self.gate.wait()
        return {"stdout": f"ok-{len(self.calls)}", "stderr": "", "exit_code": 0}


class FakeManager:
    def __init__(self) -> None:
        self.r = FakeRouter()

    def hook_token_matches(self, sid: str, token: str | None) -> bool:
        return sid == "s1" and token == TOKEN

    def router(self, sid: str) -> FakeRouter:
        if sid != "s1":
            raise InvalidState(sid, "stopped")
        return self.r


def client(mgr: FakeManager, host: str = "127.0.0.1") -> httpx.AsyncClient:
    app = create_internal_app(mgr)  # type: ignore[arg-type]
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(host, 1234)), base_url="http://t"
    )


H = {"X-CCW-Session": "s1", "X-CCW-Hook-Token": TOKEN}


async def test_routes_hook_to_session_router() -> None:
    mgr = FakeManager()
    async with client(mgr) as c:
        r = await c.post("/internal/hooks/Stop", json={"a": 1}, headers=H)
        assert r.status_code == 200
        assert r.json() == {"stdout": "ok-1", "stderr": "", "exit_code": 0}
        assert mgr.r.calls == [("Stop", {"a": 1})]
        h = await c.get("/internal/healthz")
        assert h.json() == {"status": "ok"}


async def test_unknown_event_404() -> None:
    async with client(FakeManager()) as c:
        assert (await c.post("/internal/hooks/Nope", json={}, headers=H)).status_code == 404


async def test_non_loopback_client_rejected() -> None:
    async with client(FakeManager(), host="10.1.2.3") as c:
        assert (await c.post("/internal/hooks/Stop", json={}, headers=H)).status_code == 403


async def test_ipv6_loopback_ok() -> None:
    async with client(FakeManager(), host="::1") as c:
        assert (await c.post("/internal/hooks/Stop", json={}, headers=H)).status_code == 200


async def test_wrong_token_403() -> None:
    mgr = FakeManager()
    async with client(mgr) as c:
        for headers in (
            {},
            {"X-CCW-Session": "s1"},
            {"X-CCW-Session": "s1", "X-CCW-Hook-Token": "bad"},
        ):
            assert (
                await c.post("/internal/hooks/Stop", json={}, headers=headers)
            ).status_code == 403
    assert mgr.r.calls == []


async def test_unknown_session_403() -> None:
    mgr = FakeManager()
    async with client(mgr) as c:
        headers = {"X-CCW-Session": "zz", "X-CCW-Hook-Token": TOKEN}
        assert (await c.post("/internal/hooks/Stop", json={}, headers=headers)).status_code == 403
    assert mgr.r.calls == []


async def test_bad_body_400() -> None:
    async with client(FakeManager()) as c:
        assert (await c.post("/internal/hooks/Stop", json=[1], headers=H)).status_code == 400
        r = await c.post("/internal/hooks/Stop", content=b"not json", headers=H)
        assert r.status_code == 400


async def test_duplicate_delivery_deduped() -> None:
    mgr = FakeManager()
    async with client(mgr) as c:
        h = {**H, "X-CCW-Delivery": "d1"}
        a = await c.post("/internal/hooks/Stop", json={}, headers=h)
        b = await c.post("/internal/hooks/Stop", json={}, headers=h)
        assert a.json() == b.json()
        assert len(mgr.r.calls) == 1
        await c.post("/internal/hooks/Stop", json={}, headers=H)
        await c.post("/internal/hooks/Stop", json={}, headers=H)
        assert len(mgr.r.calls) == 3


async def test_dedupe_inflight_awaits_same_result() -> None:
    mgr = FakeManager()
    mgr.r.gate = asyncio.Event()
    async with client(mgr) as c:
        h = {**H, "X-CCW-Delivery": "d2"}
        t1 = asyncio.create_task(c.post("/internal/hooks/Stop", json={}, headers=h))
        await asyncio.sleep(0.05)
        t2 = asyncio.create_task(c.post("/internal/hooks/Stop", json={}, headers=h))
        await asyncio.sleep(0.05)
        mgr.r.gate.set()
        r1, r2 = await t1, await t2
        assert r1.json() == r2.json()
        assert len(mgr.r.calls) == 1


async def test_dedupe_cache_bounded_to_256() -> None:
    mgr = FakeManager()
    async with client(mgr) as c:
        for i in range(257):
            await c.post("/internal/hooks/Stop", json={}, headers={**H, "X-CCW-Delivery": f"i{i}"})
        assert len(mgr.r.calls) == 257
        await c.post("/internal/hooks/Stop", json={}, headers={**H, "X-CCW-Delivery": "i256"})
        assert len(mgr.r.calls) == 257
        await c.post("/internal/hooks/Stop", json={}, headers={**H, "X-CCW-Delivery": "i0"})
        assert len(mgr.r.calls) == 258


async def test_no_openapi_routes(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    app = create_internal_app(FakeManager())  # type: ignore[arg-type]
    assert app.docs_url is None and app.redoc_url is None and app.openapi_url is None
    paths = {getattr(r, "path", "") for r in app.routes}
    assert paths == {"/internal/hooks/{event}", "/internal/healthz", "/internal/mcp"}
    async with client(FakeManager()) as c:
        await c.post("/internal/hooks/Stop", json={}, headers={**H, "X-CCW-Hook-Token": "x"})
        await c.post("/internal/hooks/Stop", json={}, headers=H)
    assert TOKEN not in caplog.text
    assert (await _get(app, "/docs")) == 404


async def _get(app: Any, path: str) -> int:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)), base_url="http://t"
    ) as c:
        return (await c.get(path)).status_code


async def test_pretooluse_deny_round_trip_through_relay() -> None:
    import json
    import os
    import sys

    import uvicorn

    from claude_code_wrapper.hook_relay import deny_output

    class DenyRouter(FakeRouter):
        async def handle_claude_hook(self, event: str, payload: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((event, payload))
            return {"stdout": deny_output("blocked by test"), "stderr": "", "exit_code": 0}

    mgr = FakeManager()
    mgr.r = DenyRouter()
    server = uvicorn.Server(
        uvicorn.Config(
            create_internal_app(mgr),  # type: ignore[arg-type]
            host="127.0.0.1",
            port=0,
            log_level="warning",
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started
        port = server.servers[0].sockets[0].getsockname()[1]
        env = {
            **os.environ,
            "CCW_INTERNAL_URL": f"http://127.0.0.1:{port}",
            "CCW_SESSION_ID": "s1",
            "CCW_HOOK_TOKEN": TOKEN,
            "CCW_RELAY_BACKOFF": "0.01",
        }
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "claude_code_wrapper.hook_relay",
            "PreToolUse",
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(b'{"tool_name": "Bash"}'), 30)
        assert proc.returncode == 0
        decision = json.loads(out)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert decision["permissionDecisionReason"] == "blocked by test"
        assert mgr.r.calls == [("PreToolUse", {"tool_name": "Bash"})]
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
