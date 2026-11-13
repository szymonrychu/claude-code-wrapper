"""Notice and handoff turns before a graceful shutdown (`shutdown.handoff`), on the fake claude.

On SIGTERM (the `stop` event of `run_service`), with both HTTP servers and SSE still up, every
live session gets a notice turn and a handoff turn on the regular lane, reports the handoff as
`shutdown_handoff`, and only then stops as before. A running turn is never interrupted.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from typing import Any

import httpx
from conftest import World, wait_until
from test_shutdown_turn_resume import SLOW, stored
from test_turn_continuity import of_type, transcript, user_prompts

from claude_code_wrapper.config import Config
from claude_code_wrapper.manager import CreateSessionRequest
from claude_code_wrapper.service import ServiceHandle, run_service
from claude_code_wrapper.shutdown_handoff import run_shutdown_handoff

TOKEN = "tok-handoff-abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NOTICE = "NOTICE: this pod is about to be replaced"
HANDOFF = "HANDOFF: write down your state"
SCENARIO = {
    "turns": [
        {"match": "^NOTICE", "steps": [{"text": "noted"}]},
        {"match": "^HANDOFF", "steps": [{"text": "state saved"}]},
    ]
}
NEVER_ENDING = {
    "queue_input": True,
    "turns": [
        {"match": "^NOTICE", "steps": [{"text": "noted"}]},
        {"match": "^HANDOFF", "steps": [{"text": "starting"}, {"sleep": 30}]},
    ],
}


def handoff_cfg(**kw: Any) -> dict[str, Any]:
    base = {
        "enabled": True,
        "notice_seconds": 2,
        "handoff_seconds": 3,
        "idle_proceed_seconds": 0.5,
        "notice_prompt": NOTICE,
        "handoff_prompt": HANDOFF,
    }
    return {"handoff": {**base, **kw}}


def config(world: World, scenario: dict | None = None, **handoff: Any) -> Config:
    return world.cfg(
        scenario or SCENARIO,
        server={"auth_token": TOKEN},
        shutdown=handoff_cfg(**handoff),
    )


async def serve(
    world: World, cfg: Config
) -> tuple[ServiceHandle, asyncio.Task, asyncio.Event, str]:
    cfg = cfg.model_copy(
        update={
            "server": cfg.server.model_copy(
                update={"host": "127.0.0.1", "port": 0, "internal_port": 0}
            )
        }
    )
    handle, ready, stop = ServiceHandle(), asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(run_service(cfg, ready=ready, stop=stop, handle=handle))
    await asyncio.wait_for(ready.wait(), 20)
    return handle, task, stop, f"http://127.0.0.1:{handle.public_port}"


async def new_session(pub: str) -> tuple[str, str]:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{pub}/v1/sessions", json={}, headers=AUTH)
    assert r.status_code == 201
    body = r.json()
    return body["session_id"], body["claude_session_id"]


def prompts(world: World, sid: str, claude_sid: str) -> list[str]:
    """The user prompts claude saw, without the wrapper's trailing marker comment."""
    return [
        re.sub(r"\n\n<!-- ccw .*? -->$", "", p)
        for p in user_prompts(transcript(world, sid, claude_sid))
    ]


def types(world: World, sid: str) -> list[str]:
    return [e["type"] for e in world.events(sid)]


async def test_sigterm_idle_session_gets_notice_then_handoff_then_stops(world: World) -> None:
    _, task, stop, pub = await serve(world, config(world))
    sid, claude_sid = await new_session(pub)
    stop.set()
    await asyncio.wait_for(task, 20)

    assert prompts(world, sid, claude_sid) == [NOTICE, HANDOFF]
    names = types(world, sid)
    assert names.index("shutdown_started") < names.index("shutdown_handoff")
    assert of_type(world, sid, "shutdown_started") == [{}]
    [handoff] = of_type(world, sid, "shutdown_handoff")
    assert handoff["status"] == "completed" and handoff["final_text"] == "state saved"
    assert handoff["turn_id"]
    assert world.meta(sid)["state"] == "stopped"


async def test_notice_wait_ends_early_when_idle(world: World) -> None:
    cfg = config(world, notice_seconds=30)
    m = world.manager(cfg)
    created = await m.create(CreateSessionRequest())
    sid, claude_sid = created["session_id"], created["claude_session_id"]
    began = time.monotonic()
    await run_shutdown_handoff(m, cfg.shutdown.handoff)
    assert time.monotonic() - began < 10
    assert prompts(world, sid, claude_sid) == [NOTICE, HANDOFF]


async def test_running_turn_is_never_interrupted(world: World) -> None:
    cfg = config(
        world,
        {"queue_input": True, "turns": [SLOW, *SCENARIO["turns"]]},
        notice_seconds=1,
        handoff_seconds=1,
    )
    m = world.manager(cfg)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    record = await m.router(sid).submit_turn("slow work")
    await world.until(lambda: of_type(world, sid, "assistant_text"))

    await run_shutdown_handoff(m, cfg.shutdown.handoff)

    assert of_type(world, sid, "interrupted") == []
    assert record.state == "running"
    [handoff] = of_type(world, sid, "shutdown_handoff")
    assert handoff["status"] in ("not_started", "running") and handoff["final_text"] is None
    await m.shutdown()
    assert record.interrupted == "shutdown" and record.error is None
    current = stored(world, sid).current
    assert current is not None and current.turn_id == record.turn_id
    assert (current.state, current.interrupted) == ("running", "shutdown")
    assert of_type(world, sid, "turn_failed") == []


async def test_http_and_sse_stay_up_during_routine(world: World) -> None:
    _, task, stop, pub = await serve(
        world, config(world, notice_seconds=3, idle_proceed_seconds=30)
    )
    sid, _ = await new_session(pub)
    seen: list[str] = []

    async def listen() -> None:
        async with (
            httpx.AsyncClient(timeout=30) as c,
            c.stream("GET", f"{pub}/v1/sessions/{sid}/events", headers=AUTH) as r,
        ):
            async for line in r.aiter_lines():
                if line.startswith("event: "):
                    seen.append(line[7:])

    listener = asyncio.create_task(listen())
    await wait_until(lambda: "session_state" in seen, what="sse connected")
    stop.set()
    await wait_until(lambda: "shutdown_started" in seen, 5, "shutdown_started on SSE")
    async with httpx.AsyncClient(timeout=5) as c:
        assert (await c.get(f"{pub}/healthz")).status_code == 200
    assert not task.done()
    await asyncio.wait_for(task, 20)
    listener.cancel()
    with contextlib.suppress(asyncio.CancelledError, httpx.HTTPError):
        await listener


async def test_second_sigterm_is_ignored(world: World) -> None:
    _, task, stop, pub = await serve(world, config(world))
    sid, claude_sid = await new_session(pub)
    stop.set()
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, 20)
    seen = prompts(world, sid, claude_sid)
    assert seen.count(NOTICE) == 1 and seen.count(HANDOFF) == 1


async def test_disabled_runs_todays_shutdown(world: World) -> None:
    cfg = world.cfg(
        SCENARIO,
        server={"auth_token": TOKEN},
        shutdown={"handoff": {"enabled": False, "notice_prompt": NOTICE}},
    )
    _, task, stop, pub = await serve(world, cfg)
    sid, claude_sid = await new_session(pub)
    stop.set()
    await asyncio.wait_for(task, 20)
    assert prompts(world, sid, claude_sid) == []
    assert "shutdown_started" not in types(world, sid)
    assert "shutdown_handoff" not in types(world, sid)
    assert world.meta(sid)["state"] == "stopped"


async def test_routine_bounded(world: World) -> None:
    cfg = config(world, NEVER_ENDING, notice_seconds=1, handoff_seconds=1, idle_proceed_seconds=0.2)
    m = world.manager(cfg)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    began = time.monotonic()
    await run_shutdown_handoff(m, cfg.shutdown.handoff)
    assert time.monotonic() - began < 1 + 1 + 2
    [handoff] = of_type(world, sid, "shutdown_handoff")
    assert handoff["status"] == "running" and handoff["final_text"] is None
