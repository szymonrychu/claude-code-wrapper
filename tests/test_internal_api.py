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


async def test_happy_path_and_healthz() -> None:
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


async def test_non_loopback_403() -> None:
    async with client(FakeManager(), host="10.1.2.3") as c:
        assert (await c.post("/internal/hooks/Stop", json={}, headers=H)).status_code == 403


async def test_ipv6_loopback_ok() -> None:
    async with client(FakeManager(), host="::1") as c:
        assert (await c.post("/internal/hooks/Stop", json={}, headers=H)).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-CCW-Session": "s1"},
        {"X-CCW-Session": "s1", "X-CCW-Hook-Token": "bad"},
        {"X-CCW-Session": "zz", "X-CCW-Hook-Token": TOKEN},
    ],
)
async def test_bad_auth_403(headers: dict[str, str]) -> None:
    mgr = FakeManager()
    async with client(mgr) as c:
        assert (await c.post("/internal/hooks/Stop", json={}, headers=headers)).status_code == 403
    assert mgr.r.calls == []


async def test_body_must_be_object() -> None:
    async with client(FakeManager()) as c:
        assert (await c.post("/internal/hooks/Stop", json=[1], headers=H)).status_code == 400
        r = await c.post("/internal/hooks/Stop", content=b"not json", headers=H)
        assert r.status_code == 400


async def test_dedupe_by_delivery_id() -> None:
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


async def test_no_docs_routes_and_no_token_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    app = create_internal_app(FakeManager())  # type: ignore[arg-type]
    assert app.docs_url is None and app.redoc_url is None and app.openapi_url is None
    paths = {getattr(r, "path", "") for r in app.routes}
    assert paths == {"/internal/hooks/{event}", "/internal/healthz"}
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
