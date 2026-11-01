from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from claude_code_wrapper.internal_api import (
    MAX_RATE_LIMIT_WINDOWS,
    create_internal_app,
    parse_statusline,
)
from claude_code_wrapper.manager import InvalidState

TOKEN = "tok-secret-123"


class FakeRouter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.gate: asyncio.Event | None = None
        self.statuslines: list[dict[str, Any]] = []

    def on_statusline(self, values: dict[str, Any]) -> None:
        self.statuslines.append(values)

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
    assert paths == {
        "/internal/hooks/{event}",
        "/internal/healthz",
        "/internal/mcp",
        "/internal/statusline",
    }
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


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": "lots"},
        {"input_tokens": [1]},
        {"cache_read_input_tokens": {"a": 1}},
        {"input_tokens": -5},
        {"input_tokens": True},
        {"input_tokens": 1e999},
        {"input_tokens": float("nan")},
        {"input_tokens": 10**400},
    ],
)
async def test_statusline_rejects_non_numeric_usage(usage: dict[str, Any]) -> None:
    mgr = FakeManager()
    async with client(mgr) as c:
        body = json.dumps({"context_window": {"current_usage": usage}}, allow_nan=True)
        r = await c.post(
            "/internal/statusline", content=body, headers={**H, "content-type": "application/json"}
        )
        assert r.status_code == 400
        for bad in ("lots", -1, 1e999):
            r = await c.post(
                "/internal/statusline",
                content=json.dumps({"context_window": {"context_window_size": bad}}),
                headers={**H, "content-type": "application/json"},
            )
            assert r.status_code == 400
        r = await c.post(
            "/internal/statusline",
            json={"context_window": {"current_usage": {"input_tokens": 5, "output_tokens": None}}},
            headers=H,
        )
        assert r.status_code == 204
    assert len(mgr.r.statuslines) == 1


async def test_statusline_rejects_huge_integer_percentage() -> None:
    mgr = FakeManager()
    async with client(mgr) as c:
        r = await c.post(
            "/internal/statusline",
            content='{"context_window": {"used_percentage": 1' + "0" * 400 + "}}",
            headers={**H, "content-type": "application/json"},
        )
        assert r.status_code == 400


async def test_statusline_body_cap_16k() -> None:
    mgr = FakeManager()
    async with client(mgr) as c:
        ok = json.dumps({"cwd": "x" * 10240, "rate_limits": RL})
        r = await c.post(
            "/internal/statusline", content=ok, headers={**H, "content-type": "application/json"}
        )
        assert r.status_code == 204
        pad = 16385 - len(json.dumps({"model": ""}))
        big = json.dumps({"model": "x" * pad})
        assert len(big) == 16385
        r = await c.post(
            "/internal/statusline", content=big, headers={**H, "content-type": "application/json"}
        )
        assert r.status_code == 413
        exact = json.dumps({"model": "x" * (pad - 1)})
        r = await c.post(
            "/internal/statusline", content=exact, headers={**H, "content-type": "application/json"}
        )
        assert r.status_code == 204
    assert len(mgr.r.statuslines) == 2


async def test_statusline_rejects_overflowing_token_sum() -> None:
    mgr = FakeManager()
    big = 17 * 10**307
    usage = {
        "input_tokens": big,
        "cache_creation_input_tokens": big,
        "cache_read_input_tokens": big,
    }
    async with client(mgr) as c:
        r = await c.post(
            "/internal/statusline",
            json={"context_window": {"context_window_size": 1, "current_usage": usage}},
            headers=H,
        )
        assert r.status_code == 400


# ---------------------------------------------------------------- statusLine rate_limits

RL = {
    "five_hour": {"used_percentage": 23.5, "resets_at": 1794020400},
    "seven_day": {"used_percentage": 22, "resets_at": 1794049200},
}
CONTEXT = {
    "used_percentage": 12.5,
    "context_window_size": 200000,
    "current_usage": {"input_tokens": 10},
}


def test_statusline_keeps_rate_limits() -> None:
    values = parse_statusline({"rate_limits": RL, "version": "2.1.304"})
    assert values["rate_limits"] == RL
    assert values["version"] == "2.1.304"


@pytest.mark.parametrize("bad", ["x", 1, [RL], None, True, {}])
def test_statusline_rate_limits_not_an_object(bad: Any) -> None:
    assert parse_statusline({"rate_limits": bad})["rate_limits"] is None


def test_statusline_bad_rate_limits_dropped() -> None:
    rate_limits = {
        "five_hour": RL["five_hour"],
        "pct_string": {"used_percentage": "a", "resets_at": 1},
        "pct_bool": {"used_percentage": True, "resets_at": 1},
        "pct_nan": {"used_percentage": float("nan"), "resets_at": 1},
        "pct_inf": {"used_percentage": float("inf"), "resets_at": 1},
        "pct_huge": {"used_percentage": 10**400, "resets_at": 1},
        "pct_negative": {"used_percentage": -1, "resets_at": 1},
        "resets_missing": {"used_percentage": 1},
        "resets_zero": {"used_percentage": 1, "resets_at": 0},
        "resets_string": {"used_percentage": 1, "resets_at": "x"},
        "resets_bool": {"used_percentage": 1, "resets_at": True},
        "Bad-Key": {"used_percentage": 1, "resets_at": 1},
        "k" * 65: {"used_percentage": 1, "resets_at": 1},
        "spend_limit": {"monthly_limit": 100, "used": 3},
        "not_an_object": 5,
        "seven_day": {**RL["seven_day"], "extra": "dropped"},
        "zero_ok": {"used_percentage": 0, "resets_at": 1.5},
    }
    values = parse_statusline({"rate_limits": rate_limits})
    assert values["rate_limits"] == {
        **RL,
        "zero_ok": {"used_percentage": 0, "resets_at": 1.5},
    }
    only_bad = {k: v for k, v in rate_limits.items() if k not in (*RL, "zero_ok")}
    assert parse_statusline({"rate_limits": only_bad})["rate_limits"] is None


def test_statusline_rate_limits_out_of_range_dropped() -> None:
    rate_limits = {
        "pct_over": {"used_percentage": 1000.5, "resets_at": 1794020400},
        "pct_big": {"used_percentage": 10**308, "resets_at": 1794020400},
        "resets_far": {"used_percentage": 1, "resets_at": 10**308},
        "resets_ms": {"used_percentage": 1, "resets_at": 1794020400000},
        "edge_ok": {"used_percentage": 1000, "resets_at": 4102444800},
    }
    kept = parse_statusline({"rate_limits": rate_limits})["rate_limits"]
    assert kept == {"edge_ok": {"used_percentage": 1000, "resets_at": 4102444800}}


def test_statusline_rate_limits_worst_case_body_fits_ingest_cap() -> None:
    """Every kept window at its widest still serializes far below the 8192-byte ingest cap."""
    widest = {"used_percentage": 999.9999999999999, "resets_at": 4102444799.9999995}
    many = {f"{i:02d}" + "k" * 62: widest for i in range(40)}
    kept = parse_statusline({"rate_limits": many})["rate_limits"]
    assert len(kept) == 16
    body = {"agent": "a" * 128, "cli_version": "v" * 64, "rate_limits": kept}
    assert len(json.dumps(body)) <= 8192


def test_statusline_version_bounds() -> None:
    assert parse_statusline({"version": "v" * 64})["version"] == "v" * 64
    for bad in ("v" * 65, 2, None, "", ["2.1.304"]):
        assert parse_statusline({"version": bad})["version"] is None


def test_statusline_rate_limits_capped_keeps_known_windows() -> None:
    many = {f"w{i:02d}": {"used_percentage": i, "resets_at": 1794000000 + i} for i in range(40)}
    # the well-known windows come last, so a plain "first 16" would lose them
    rate_limits = {**many, **RL}
    kept = parse_statusline({"rate_limits": rate_limits})["rate_limits"]
    assert len(kept) == MAX_RATE_LIMIT_WINDOWS == 16
    assert kept["five_hour"] == RL["five_hour"] and kept["seven_day"] == RL["seven_day"]
    # the rest are the first valid ones in payload order
    assert [k for k in kept if k not in RL] == [f"w{i:02d}" for i in range(14)]
    assert len(json.dumps(kept)) < 8192


def test_statusline_without_rate_limits() -> None:
    values = parse_statusline({"context_window": CONTEXT, "model": "m"})
    assert values == {
        **CONTEXT,
        "model": "m",
        "rate_limits": None,
        "version": None,
    }


class Offers:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any]] = []

    def offer(self, rate_limits: Any, cli_version: Any) -> None:
        self.calls.append((rate_limits, cli_version))


class RealManager(FakeManager):
    """A FakeManager whose router is a real SessionRouter, optionally wired to `offers`."""

    def __init__(self, tmp_path: Path, offers: Offers | None) -> None:
        from test_router import Harness

        self.h = Harness(tmp_path)
        if offers is not None:
            self.h.router.on_rate_limits = offers.offer
        self.r = self.h.router


async def test_route_offers_rate_limits(tmp_path: Path) -> None:
    offers = Offers()
    mgr = RealManager(tmp_path, offers)
    try:
        async with client(mgr) as c:
            body = {"context_window": CONTEXT, "rate_limits": RL, "version": "2.1.304"}
            r = await c.post("/internal/statusline", json=body, headers=H)
            assert r.status_code == 204
        assert mgr.h.router.context is not None
        assert mgr.h.router.context["used_percentage"] == 12.5
        assert offers.calls == [(RL, "2.1.304")]
    finally:
        await mgr.h.router.close()


async def test_route_bad_rate_limits_still_204(tmp_path: Path) -> None:
    offers = Offers()
    mgr = RealManager(tmp_path, offers)
    try:
        async with client(mgr) as c:
            for bad in ("x", {"five_hour": {"used_percentage": "a", "resets_at": 1}}):
                body = {"context_window": CONTEXT, "rate_limits": bad, "version": "2.1.304"}
                r = await c.post("/internal/statusline", json=body, headers=H)
                assert r.status_code == 204
        assert mgr.h.router.context is not None
        assert mgr.h.router.context["used_percentage"] == 12.5
        assert offers.calls == []
    finally:
        await mgr.h.router.close()


async def test_route_no_forwarder(tmp_path: Path) -> None:
    mgr = RealManager(tmp_path, None)
    try:
        async with client(mgr) as c:
            body = {"context_window": CONTEXT, "rate_limits": RL, "version": "2.1.304"}
            r = await c.post("/internal/statusline", json=body, headers=H)
            assert r.status_code == 204
        assert mgr.h.router.context is not None
        assert mgr.h.router.on_rate_limits is None
    finally:
        await mgr.h.router.close()
