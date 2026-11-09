"""Idle footer dialogs get one guarded ESC after dialog_auto_dismiss_seconds (agent-dialogs W8b)."""

from __future__ import annotations

import asyncio
from typing import Any

from conftest import World, jsonl
from test_api_dialog import TOKEN, USAGE_FOOTER, keys
from test_permission_relay import GATED, of_type, requested

from claude_code_wrapper.manager import CommandsOverride, CreateSessionRequest

MODEL = "Select model\n 1. Default\n Esc to cancel"
SECONDS = 0.4
FIRES_ONCE = 1.0


def scen(footer: str, sleep: float = 30) -> dict:
    return {"turns": [{"match": "go", "steps": [{"text": footer}, {"sleep": sleep}]}]}


async def start(world: World, scenario: dict, seconds: float | None = SECONDS, **hold: Any):
    cfg = world.cfg(scenario, server={"auth_token": TOKEN})
    m = world.manager(cfg)
    over = CommandsOverride(dialog_auto_dismiss_seconds=seconds, **hold)
    sid = (await m.create(CreateSessionRequest(commands=over)))["session_id"]
    return m, sid, m.router(sid)


IDLE: dict = {"turns": []}


def show(session: Any, text: str) -> None:
    """Footer text on claude's screen, as the PTY reader hands it over."""
    data = text.encode()
    session.pty.ring.append(data)
    session._on_output(session.pty, data)


async def open_it(world: World, router: Any, footer: str = MODEL) -> str:
    """An idle footer dialog: no turn runs, so the waiting reason is `dialog`."""
    session = router.session
    await world.until(lambda: session.state == "ready", 15)
    show(session, footer)
    assert session.waiting_reason() == "dialog"
    return session.waiting_since


async def test_fires_once_after_timeout(world: World) -> None:
    m, sid, router = await start(world, IDLE, seconds=FIRES_ONCE)
    since = await open_it(world, router)
    assert keys(world, sid) == []
    await world.until(lambda: of_type(world, sid, "dialog_auto_dismissed"), 10)
    await world.until(lambda: len(keys(world, sid)) >= 1, 5)
    assert of_type(world, sid, "dialog_auto_dismissed") == [{"since": since, "escape_sent": True}]
    assert len(keys(world, sid)) == 1
    assert router.session.dismissed(since)


async def test_rearms_when_runner_active_at_fire_time(world: World, monkeypatch: Any) -> None:
    m, sid, router = await start(world, IDLE)
    active = {"on": True}
    monkeypatch.setattr(type(router), "runner_active", property(lambda self: active["on"]))
    since = await open_it(world, router)
    await asyncio.sleep(SECONDS + 0.3)
    assert of_type(world, sid, "dialog_auto_dismissed") == []
    assert router._dd_handle is not None  # re-armed, not dropped
    active["on"] = False
    await world.until(lambda: of_type(world, sid, "dialog_auto_dismissed"), 10)
    assert of_type(world, sid, "dialog_auto_dismissed")[0]["since"] == since


async def test_not_fired_when_dialog_closed_first(world: World) -> None:
    m, sid, router = await start(world, IDLE, seconds=1.5)
    await open_it(world, router)
    router.session._ui_dialog_at = float("-inf")  # the footer went away
    router.session._sync_waiting()
    assert router._dd_handle is None
    await asyncio.sleep(1.8)
    assert of_type(world, sid, "dialog_auto_dismissed") == []
    assert keys(world, sid) == []


async def test_not_fired_for_usage_wait(world: World) -> None:
    m, sid, router = await start(world, IDLE)
    show(router.session, USAGE_FOOTER)
    assert router.session.waiting_since is not None
    await asyncio.sleep(SECONDS + 0.6)
    assert of_type(world, sid, "dialog_auto_dismissed") == []
    assert keys(world, sid) == []


async def test_not_armed_while_a_turn_runs(world: World) -> None:
    """A footer while a turn runs is waiting reason `permission` (a TUI prompt or question with
    no relay): only an idle `dialog` gets the automatic ESC, which would interrupt the turn."""
    m, sid, router = await start(world, scen(MODEL))
    await router.submit_turn("go")
    await world.until(lambda: router.session.dialog_open(), 15)
    assert router.session.waiting_reason() == "permission"
    assert router.session.dialog_kind() == "dialog"
    assert router._dd_handle is None
    await asyncio.sleep(SECONDS + 0.6)
    assert of_type(world, sid, "dialog_auto_dismissed") == []
    assert keys(world, sid) == []


async def test_not_fired_for_held_permission(world: World) -> None:
    m, sid, router = await start(world, GATED)
    await router.submit_turn("go")
    view = await requested(world, sid)
    await asyncio.sleep(SECONDS + 0.6)
    assert of_type(world, sid, "dialog_auto_dismissed") == []
    assert keys(world, sid) == []
    router.answer_permission(view["request_id"], "deny", None)
    await world.wait_turn(m, sid)


async def test_zero_disables(world: World) -> None:
    m, sid, router = await start(world, IDLE, seconds=0)
    await open_it(world, router)
    assert router._dd_handle is None
    await asyncio.sleep(0.8)
    assert of_type(world, sid, "dialog_auto_dismissed") == []
    assert keys(world, sid) == []


async def test_cancelled_on_close(world: World) -> None:
    m, sid, router = await start(world, IDLE, seconds=1.0)
    await open_it(world, router)
    assert router._dd_handle is not None
    await router.close()
    assert router._dd_handle is None
    await asyncio.sleep(1.3)
    assert of_type(world, sid, "dialog_auto_dismissed") == []
    assert jsonl(world.session_dir(sid) / "config" / "fake-keys.jsonl") == []
