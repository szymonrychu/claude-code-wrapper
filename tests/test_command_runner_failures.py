"""Command runner failures on an idle session (design D4.6 idle path, D6 shutdown)."""

from __future__ import annotations

import os
import signal
from typing import Any

import httpx
from conftest import World
from test_command_runner import (
    TOKEN,
    commands_cfg,
    finished,
    inputs,
    of_type,
    start,
    types,
)

from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.commands import CommandRejected
from claude_code_wrapper.manager import CreateSessionRequest
from claude_code_wrapper.metrics import Metrics

HANG = {"commands": {"reload-skills": {"hang": True}, "context": {"hang": True}}}


def failed(world: World, sid: str) -> list[tuple[str, str, str | None]]:
    return [
        (e["data"]["record"]["name"], e["data"]["record"]["state"], e["data"]["record"]["error"])
        for e in world.events(sid)
        if e["type"] == "command_failed"
    ]


async def running_pair(world: World, **kw: Any) -> tuple[Any, str, Any, Any, Any]:
    m, sid, _ = await start(world, HANG, **kw)
    router = m.router(sid)
    first = router.request_command("reload-skills", origin="client")
    second = router.request_command("context", origin="client")
    await world.until(lambda: first.state == "running")
    return m, sid, router, first, second


async def test_crash_fails_running_and_cancels_queued(world):
    m, sid, router, first, second = await running_pair(world)
    os.kill(router.session.pty.pid, signal.SIGKILL)
    await finished(world, first)
    await finished(world, second)
    assert (first.state, first.error) == ("failed", "claude_exited")
    assert second.state == "cancelled"
    assert failed(world, sid) == [
        ("reload-skills", "failed", "claude_exited"),
        ("context", "cancelled", None),
    ]
    await world.until(lambda: not router.runner_active)
    await world.until(lambda: m.get(sid)["state"] == "ready" and m.get(sid)["restarts"] == 1)
    # After the relaunch the session accepts new commands and turns.
    nxt = router.request_command("context", origin="client")
    await world.until(lambda: nxt.state == "running")
    assert router.session.state == "busy"


async def test_crash_then_ready_accepts_turns(world):
    m, sid, router, first, second = await running_pair(world)
    os.kill(router.session.pty.pid, signal.SIGKILL)
    await finished(world, second)
    await world.until(lambda: m.get(sid)["state"] == "ready" and m.get(sid)["restarts"] == 1)
    record = await router.submit_turn("hello")
    await world.until(lambda: record.state != "running")
    assert record.state == "completed"


async def test_destroy_fails_running_and_closes_once(world):
    m, sid, router, first, second = await running_pair(world)
    await m.destroy(sid)
    assert (first.state, first.error) == ("failed", "session_stopped")
    assert second.state == "cancelled"
    assert not router.runner_active
    assert types(world, sid, "session_").count("session_closed") == 1
    assert [t for t in types(world, sid, "command_")].count("command_failed") == 2
    assert second.started_at is None  # never typed


async def test_shutdown_fails_running_command(world):
    m, sid, router, first, second = await running_pair(world)
    await m.shutdown()
    assert (first.state, first.error) == ("failed", "session_stopped")
    assert second.state == "cancelled"
    assert types(world, sid, "session_").count("session_closed") == 1


async def test_close_drain_ends_runner_and_types_nothing_more(world):
    m, sid, router, first, second = await running_pair(world)
    await router.close(drain_timeout=1.0)
    assert not router.runner_active
    assert (first.state, first.error) == ("failed", "session_stopped")
    assert second.state == "cancelled"
    assert second.started_at is None  # never typed


async def test_turn_during_idle_command_is_busy_and_drained_runner_accepts(world):
    cfg = world.cfg(
        {"commands": {"reload-skills": {"delay_seconds": 0.8}}},
        server={"auth_token": TOKEN},
        sessions={"commands": commands_cfg()},
    )
    metrics = Metrics()
    m = world.manager(cfg, metrics=metrics)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    router = m.router(sid)
    transport = httpx.ASGITransport(app=create_app(m, cfg, metrics))
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        rec = router.request_command("reload-skills", origin="client")
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "early"})
        assert r.status_code == 409 and r.json()["error"]["code"] == "busy"
        await finished(world, rec)
        await world.until(lambda: router.session.state == "ready")
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "late"})
        assert r.status_code == 202
    assert not [i for i in inputs(world, sid) if i["text"] == "early"]


async def test_command_while_not_ready_is_invalid_state(world):
    m, sid, _ = await start(world)
    router = m.router(sid)
    for state in ("booting", "dead"):
        router.session.state = state
        try:
            router.request_command("context", origin="client")
        except CommandRejected as exc:
            assert exc.code == "invalid_state"
        else:
            raise AssertionError(f"{state}: not rejected")
    router.session.state = "ready"
    assert router.commands.list() == []
    assert of_type(world, sid, "command_queued") == []


async def test_interrupt_idle_command_fails_it_and_returns_ready(world):
    m, sid, router, first, second = await running_pair(world)
    assert await router.interrupt() is None
    assert (first.state, first.error) == ("failed", "interrupted")
    assert second.state == "cancelled"
    assert router.session.state == "ready" and not router.runner_active
    assert failed(world, sid) == [
        ("reload-skills", "failed", "interrupted"),
        ("context", "cancelled", None),
    ]
    again = router.request_command("context", origin="client")
    await world.until(lambda: again.state == "running")
