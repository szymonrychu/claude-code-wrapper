"""The `user_message` event: every prompt pasted into the session, with its origin (A2)."""

from __future__ import annotations

import pytest
from conftest import World
from test_command_runner import finished
from test_command_runner import start as start_commands
from test_command_turn_resume import CONTINUE
from test_command_turn_resume import start as start_resume
from test_stop_gate import Gate
from test_stop_gate import start as start_stop
from test_stop_gate import turn as stop_turn
from test_turn_continuity import CRASH, RESUME
from test_turn_continuity import start as start_restart

from claude_code_wrapper.events import EVENT_TYPES


def of_type(world: World, sid: str, type_: str) -> list[dict]:
    return [e for e in world.events(sid) if e["type"] == type_]


def order(world: World, sid: str, *wanted: str) -> list[str]:
    return [e["type"] for e in world.events(sid) if e["type"] in wanted]


def test_user_message_is_an_event_type() -> None:
    assert "user_message" in EVENT_TYPES


async def test_user_turn_emits_user_message(world: World) -> None:
    m, _, sid, _ = await start_restart(world, [{"match": "^hi", "steps": [{"text": "yo"}]}])
    record = await m.router(sid).submit_turn("hi there")
    await world.wait_turn(m, sid)
    (ev,) = of_type(world, sid, "user_message")
    assert ev["data"] == {
        "turn_id": record.turn_id,
        "origin": "client",
        "text": "hi there",
        "files": [],
    }
    busy = [
        i
        for i, e in enumerate(world.events(sid))
        if e["type"] == "session_state" and e["data"]["state"] == "busy"
    ]
    types = [e["type"] for e in world.events(sid)]
    assert busy[0] < types.index("user_message") < types.index("turn_started")


async def test_peer_origin(world: World) -> None:
    m, _, sid, _ = await start_restart(world, [{"match": "^hi", "steps": [{"text": "yo"}]}])
    # peer_inbox._attempt submits exactly this way.
    record = await m.router(sid).submit_turn("hi from a peer", origin="peer")
    await world.wait_turn(m, sid)
    (ev,) = of_type(world, sid, "user_message")
    assert ev["data"]["origin"] == "peer" and ev["data"]["turn_id"] == record.turn_id
    assert ev["data"]["text"] == "hi from a peer" and ev["data"]["files"] == []


async def test_restart_continue_origin_and_text(world: World) -> None:
    m, _, sid, _ = await start_restart(world, [CRASH, RESUME])
    record = await m.router(sid).submit_turn("go now")
    await world.until(lambda: record.state != "running")
    msgs = [e["data"] for e in of_type(world, sid, "user_message")]
    assert [(d["origin"], d["turn_id"]) for d in msgs] == [
        ("client", record.turn_id),
        ("restart", record.turn_id),
    ]
    assert msgs[1]["text"].startswith("Resume: claude restarted mid-turn")
    assert msgs[1]["files"] == []


async def test_command_continuation_paste(world: World) -> None:
    m, sid, router = await start_resume(world)
    turn = await router.submit_turn("go")
    router.request_command("compact", origin="agent")
    await world.wait_turn(m, sid)
    msgs = [e["data"] for e in of_type(world, sid, "user_message")]
    # The compact line itself is a command paste; the continuation follows with the turn's origin.
    assert [d["origin"] for d in msgs] == ["client", "command", "client"]
    assert msgs[2]["turn_id"] == turn.turn_id and msgs[2]["text"].startswith(CONTINUE)
    assert order(world, sid, "turn_resumed", "user_message", "turn_completed")[-2:] == [
        "user_message",
        "turn_completed",
    ]


async def test_slash_command_origin_command(world: World) -> None:
    m, sid, _ = await start_commands(world)
    rec = m.router(sid).request_command("compact", origin="client")
    await finished(world, rec)
    (ev,) = of_type(world, sid, "user_message")
    assert ev["data"]["origin"] == "command" and ev["data"]["turn_id"] is None
    assert ev["data"]["text"].startswith("/compact") and ev["data"]["files"] == []
    seen = order(world, sid, "command_started", "user_message", "command_completed")
    assert seen == ["command_started", "user_message", "command_completed"]


async def test_event_order(world: World) -> None:
    m, _, sid, _ = await start_restart(world, [{"match": "^hi", "steps": [{"text": "yo"}]}])
    record = await m.router(sid).submit_turn("hi")
    await world.wait_turn(m, sid)
    evs = world.events(sid)
    idx = {}
    for i, e in enumerate(evs):
        key = (e["type"], e["data"].get("state"))
        idx.setdefault(key, i)
    um = next(
        i
        for i, e in enumerate(evs)
        if e["type"] == "user_message" and e["data"]["turn_id"] == record.turn_id
    )
    done = next(i for i, e in enumerate(evs) if e["type"] == "turn_completed")
    assert idx[("session_state", "busy")] < um < done


async def test_turn_started_text_unchanged(world: World) -> None:
    m, _, sid, _ = await start_restart(world, [{"match": "^hi", "steps": [{"text": "yo"}]}])
    await m.router(sid).submit_turn("hi exactly")
    await world.wait_turn(m, sid)
    (ev,) = of_type(world, sid, "turn_started")
    assert ev["data"]["text"] == "hi exactly" and ev["data"]["origin"] == "client"
    assert set(ev["data"]) == {"turn_id", "origin", "text"}


async def test_turn_and_snapshot_fields_pinned(world: World) -> None:
    m, _, sid, _ = await start_restart(world, [{"match": "^hi", "steps": [{"text": "yo"}]}])
    await m.router(sid).submit_turn("hi")
    await world.wait_turn(m, sid)
    (ev,) = of_type(world, sid, "turn_completed")
    rec = ev["data"]["record"]
    assert rec["origin"] == "client" and rec["final_text"] == "yo"
    assert "usage" in rec
    snap = m.get(sid)
    assert {"current_turn_id", "model", "workdir"} <= set(snap)
    assert snap["current_turn_id"] is None and snap["workdir"]


async def test_files_are_the_written_file_names(world: World) -> None:
    m, _, sid, _ = await start_restart(world, [{"match": "^hi", "steps": [{"text": "yo"}]}])
    record = await m.router(sid).submit_turn("hi files", files={"a.txt": "A", "sub/b.txt": "B"})
    await world.wait_turn(m, sid)
    (ev,) = of_type(world, sid, "user_message")
    assert ev["data"]["turn_id"] == record.turn_id
    assert ev["data"]["files"] == ["a.txt", "sub/b.txt"]


@pytest.mark.parametrize("origin", ["initial", "agent"])
async def test_initial_and_agent_origins(world: World, origin: str) -> None:
    m, _, sid, _ = await start_restart(world, [{"match": "^hi", "steps": [{"text": "yo"}]}])
    record = await m.router(sid).submit_turn("hi there", origin=origin)
    await world.wait_turn(m, sid)
    (ev,) = of_type(world, sid, "user_message")
    assert ev["data"]["origin"] == origin and ev["data"]["turn_id"] == record.turn_id


async def test_stop_hook_block_emits_no_user_message(world: World) -> None:
    gate = Gate(world.tmp)
    gate.reply(1, {"decision": "block", "reason": "write the outcome file"})
    scenario = stop_turn([{"text": "first leg"}], on_block=[{"text": "second leg"}])
    m, sid = await start_stop(world, world.cfg(scenario, hooks={"onStop": [gate.action()]}))
    await m.router(sid).submit_turn("go")
    await world.until(lambda: of_type(world, sid, "turn_continued"))
    await world.wait_turn(m, sid)
    assert [e["data"]["origin"] for e in of_type(world, sid, "user_message")] == ["client"]
