"""A prompt (skill, plugin) command is promoted to a real turn (design D8).

claude sends such a command to the model: it fires UserPromptSubmit, runs a model turn and ends
it with Stop, and writes no command output. The runner adopts that turn on the prompt signal: the
command completes with result `turn` and the turn's id, and the turn ends like any turn. In the
same-turn path (commands run at a held turn's Stop) the held turn continues as the command's turn.
A command that claude sends to the model with no echo at all (`/tmp`, a name that exists on disk)
is promoted the same way.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from conftest import World, turns_of
from test_command_runner import ALL, TOKEN, commands_cfg, finished, inputs, keys, of_type

from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.manager import CreateSessionRequest, SessionManager

CONTINUE = "Resume after commands"
#: Far beyond every wait below: a command that only completes at its deadline fails the test.
TIMEOUT = 30
HELLO = {"match": "^/hello", "steps": [{"text": "Hello, world!"}]}
SLOW_HELLO = {"match": "^/hello", "steps": [{"sleep": 5}, {"text": "late"}]}
GO = {"match": "^go", "steps": [{"sleep": 0.8}, {"text": "part one"}]}
FIRST = {"match": "^first", "steps": [{"text": "first done"}]}
RESUMED = {"match": "^Resume after commands", "steps": [{"text": "part two"}]}
TMP = {"match": "^/tmp", "steps": [{"text": "a directory"}]}
PROMPTS = {
    "hello": {"prompt": True, "body": "Say hello to $ARGUMENTS in one short sentence."},
    "tmp": {"prose": True},
}


async def start(
    world: World, turns: list[dict], **sections: Any
) -> tuple[SessionManager, str, Any]:
    cmds = commands_cfg(
        client=[*ALL, "hello", "tmp"],
        agent=["compact", "reload-plugins", "hello"],
        continue_prompt=CONTINUE,
        timeout_seconds={"compact": 5, "default": TIMEOUT},
    )
    sessions = {"commands": cmds, **sections.pop("sessions", {})}
    cfg = world.cfg({"turns": turns, "commands": PROMPTS}, sessions=sessions, **sections)
    m = world.manager(cfg)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    return m, sid, m.router(sid)


def typed(world: World, sid: str) -> list[str]:
    return [i["text"] for i in inputs(world, sid)]


def seen(world: World, sid: str, *wanted: str) -> list[str]:
    return [e["type"] for e in world.events(sid) if e["type"] in wanted]


def stale_stops(caplog: Any) -> list[str]:
    return [r.getMessage() for r in caplog.records if "ignoring stale Stop" in r.getMessage()]


async def test_prompt_command_promotes_to_turn(world, caplog):
    caplog.set_level(logging.INFO, logger="claude_code_wrapper")
    m, sid, router = await start(world, [HELLO])
    rec = router.request_command("hello", "world", origin="client")
    await finished(world, rec, 15)
    assert (rec.state, rec.result, rec.error) == ("completed", "turn", None)
    assert rec.turn_id is not None
    done = await world.wait_turn(m, sid)
    assert done.turn_id == rec.turn_id
    assert (done.origin, done.text, done.final_text) == ("client", "/hello world", "Hello, world!")
    assert [t.turn_id for t in turns_of(m, sid)] == [rec.turn_id]
    assert of_type(world, sid, "turn_started") == [
        {"turn_id": rec.turn_id, "origin": "client", "text": "/hello world"}
    ]
    assert {"turn_id": rec.turn_id, "origin": "client", "text": "/hello world", "files": []} in (
        of_type(world, sid, "user_message")
    )
    (completed,) = of_type(world, sid, "command_completed")
    assert completed["record"]["result"] == "turn"
    assert completed["record"]["turn_id"] == rec.turn_id
    order = ("command_started", "turn_started", "command_completed", "turn_completed")
    assert seen(world, sid, *order) == list(order)
    assert stale_stops(caplog) == []
    await world.until(lambda: router.session.state == "ready")
    assert router.session.current_turn is None and not router.session.command_active


async def test_prompt_command_turn_is_interruptible(world):
    m, sid, router = await start(world, [SLOW_HELLO], server={"auth_token": TOKEN})
    app = create_app(m, m.cfg)
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    rec = router.request_command("hello", "world", origin="client")
    await finished(world, rec, 15)
    assert rec.result == "turn"
    turn = router.session.current_turn
    assert turn is not None and turn.turn_id == rec.turn_id
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        r = await c.post(f"/v1/sessions/{sid}/interrupt")
    assert r.status_code == 200
    assert r.json() == {"interrupted": True, "turn_id": rec.turn_id}
    assert turn.state == "interrupted"
    (failed,) = of_type(world, sid, "turn_failed")
    assert failed["record"]["turn_id"] == rec.turn_id
    assert failed["record"]["state"] == "interrupted"
    await world.until(lambda: router.session.state == "ready")
    assert m.get(sid)["state"] == "ready"


async def test_queued_commands_wait_for_promoted_turn(world):
    slow = {"match": "^/hello", "steps": [{"sleep": 1.5}, {"text": "Hello, world!"}]}
    m, sid, router = await start(world, [slow, RESUMED])
    order: list[tuple[str, str]] = []
    relay, send = router.handle_claude_hook, router.session.send_command

    async def answered(event: str, payload: Any) -> Any:
        out = await relay(event, payload)
        if event == "Stop":
            order.append(("stop", ""))
        return out

    async def typing(line: str) -> None:
        order.append(("typed", line))
        await send(line)

    router.handle_claude_hook = answered
    router.session.send_command = typing
    hello = router.request_command("hello", "world", origin="client")
    queued = router.request_command("reload-skills", origin="client")
    await finished(world, hello, 15)
    assert hello.result == "turn"
    # The runner stopped at the promotion: the next command waits for the turn's Stop.
    assert queued.state == "queued"
    assert router.session.current_turn is not None
    assert order == [("typed", "/hello world")]
    await finished(world, queued, 15)
    assert (queued.state, queued.result) == ("completed", "ok")
    assert order[:3] == [("typed", "/hello world"), ("stop", ""), ("typed", "/reload-skills")]
    await world.until(lambda: router.session.state == "ready" and not router.runner_active)
    assert {i["state"] for i in inputs(world, sid)} == {"idle"}


async def test_agent_prompt_command_continues_held_turn(world):
    m, sid, router = await start(world, [GO, HELLO, RESUMED])
    turn = await router.submit_turn("go")
    rec = router.request_command("hello", "world", origin="agent")
    assert rec.turn_id == turn.turn_id
    done = await world.wait_turn(m, sid)
    assert done.turn_id == turn.turn_id and done.final_text == "Hello, world!"
    assert (rec.state, rec.result, rec.turn_id) == ("completed", "turn", turn.turn_id)
    assert [t.turn_id for t in turns_of(m, sid)] == [turn.turn_id]
    assert len(of_type(world, sid, "turn_started")) == 1
    assert of_type(world, sid, "turn_resumed") == []
    assert not [t for t in typed(world, sid) if t.startswith(CONTINUE)]
    assert [t.split("\n")[0] for t in typed(world, sid)] == ["go", "/hello world"]
    await world.until(lambda: router.session.state == "ready")


async def test_interrupt_on_continued_held_turn_sends_escape(world):
    m, sid, router = await start(world, [GO, SLOW_HELLO, RESUMED])
    turn = await router.submit_turn("go")
    rec = router.request_command("hello", "world", origin="agent")
    await finished(world, rec, 15)
    assert (rec.result, rec.turn_id) == ("turn", turn.turn_id)
    session = router.session
    assert session.commands_turn is None  # the hold was released for the continued turn
    assert session.current_turn is turn and turn.state == "running"
    assert keys(world, sid) == []
    assert await router.interrupt() == turn.turn_id
    assert turn.state == "interrupted"
    assert [k["key"] for k in keys(world, sid)] == ["esc"]
    await world.until(lambda: session.state == "ready")
    (failed,) = of_type(world, sid, "turn_failed")
    assert failed["record"]["state"] == "interrupted"
    assert not [t for t in typed(world, sid) if t.startswith(CONTINUE)]


async def test_promoted_turn_after_earlier_turn_completes(world, caplog):
    caplog.set_level(logging.INFO, logger="claude_code_wrapper")
    m, sid, router = await start(world, [FIRST, HELLO])
    first = await router.submit_turn("first")
    await world.until(lambda: first.state == "completed")
    await world.until(lambda: router.session.state == "ready")
    rec = router.request_command("hello", "world", origin="client")
    await finished(world, rec, 15)
    assert rec.result == "turn"
    await world.until(lambda: len(of_type(world, sid, "turn_completed")) == 2)
    completed = [e["record"] for e in of_type(world, sid, "turn_completed")]
    assert [r["turn_id"] for r in completed] == [first.turn_id, rec.turn_id]
    assert completed[1]["final_text"] == "Hello, world!"
    assert stale_stops(caplog) == []
    await world.until(lambda: router.session.state == "ready")


async def test_prose_dispatched_command_promotes_to_turn(world):
    m, sid, router = await start(world, [TMP])
    began = time.monotonic()
    rec = router.request_command("tmp", origin="client")
    await finished(world, rec, 15)
    assert (rec.state, rec.result) == ("completed", "turn")
    done = await world.wait_turn(m, sid)
    assert done.turn_id == rec.turn_id and done.final_text == "a directory"
    await world.until(lambda: router.session.state == "ready")
    # Ready within the fake turn, never at the command's deadline.
    assert time.monotonic() - began < TIMEOUT / 3
    assert typed(world, sid) == ["/tmp"]
