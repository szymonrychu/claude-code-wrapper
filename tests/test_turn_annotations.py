"""K2 hook annotations: merged into the turn record from turn-scoped events, capped at 16 KiB."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import World, wait_turn

from claude_code_wrapper.manager import CreateSessionRequest
from claude_code_wrapper.turns import TurnRecord, TurnStore


def say(obj: dict[str, Any]) -> dict[str, Any]:
    return {"type": "command", "run": f"printf '%s' '{json.dumps(obj)}'", "blocking": True}


async def start(world: World, scenario: dict, hooks: dict) -> tuple[Any, str]:
    m = world.manager(world.cfg(scenario, hooks=hooks))
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    return m, sid


def events(world: World, sid: str, type_: str) -> list[dict[str, Any]]:
    return [e["data"] for e in world.events(sid) if e["type"] == type_]


def test_old_turns_jsonl_loads_without_annotations(tmp_path: Path):
    path = tmp_path / "turns.jsonl"
    old = {"turn_id": "t1", "session_id": "s", "started_at": "2026-01-01T00:00:00Z"}
    path.write_text(json.dumps(old) + "\n")
    store = TurnStore(path)
    assert store.get("t1").annotations == {}
    record = TurnRecord(turn_id="t2", session_id="s", started_at="x", annotations={"a": [1]})
    store.put(record)
    assert TurnStore(path).get("t2").annotations == {"a": [1]}


async def test_tool_hook_annotations_merged(world: World):
    scenario = {"turns": [{"match": "go", "steps": [{"tool": "Bash", "input": {"command": "ls"}}]}]}
    hooks = {
        "onTurn": [say({"annotations": {"turn": 1, "shared": "onTurn"}})],
        "onAgentToolCall": [say({"annotations": {"call": 1, "shared": "call"}})],
        "onToolResult": [say({"annotations": {"result": 1}})],
        "onStop": [say({"annotations": {"stop": 1, "shared": "stop"}})],
        "onTurnComplete": [say({"annotations": {"complete": 1}})],
        "onAgentResponse": [say({"annotations": {"ignored": 1}})],
    }
    m, sid = await start(world, scenario, hooks)
    router = m.router(sid)
    record = await router.submit_turn("go", callback_url=f"{world.url}/cb")
    assert record.annotations == {"turn": 1, "shared": "onTurn"}
    await wait_turn(m, sid)
    await router.drain(5)
    await world.until(lambda: world.callbacks)
    want = {"turn": 1, "call": 1, "result": 1, "stop": 1, "shared": "stop"}
    (event,) = events(world, sid, "turn_completed")
    assert event["record"]["annotations"] == want
    assert world.callbacks[0]["data"]["record"]["annotations"] == want
    stored = m.open_turns(sid).get(record.turn_id)
    assert stored.annotations == want | {"complete": 1}
    assert "ignored" not in stored.annotations


async def test_on_stop_annotations_merged_on_block_and_defer_legs(world: World):
    scenario = {"turns": [{"match": "go", "steps": [{"text": "a"}], "on_block": [{"text": "b"}]}]}
    run = (
        'f="$CCW_STATE_DIR/seen"; if [ -f "$f" ]; '
        "then printf '%s' '{\"annotations\":{\"leg\":2}}'; "
        'else touch "$f"; printf \'%s\' \'{"decision":"block","reason":"again",'
        '"annotations":{"leg":1,"first":true}}\'; fi'
    )
    hooks = {"onStop": [{"type": "command", "run": run, "blocking": True}]}
    m, sid = await start(world, scenario, hooks)
    await m.router(sid).submit_turn("go")
    done = await wait_turn(m, sid)
    assert done.annotations == {"leg": 2, "first": True}
    (event,) = events(world, sid, "turn_completed")
    assert event["record"]["annotations"] == {"leg": 2, "first": True}


async def test_annotations_size_cap(world: World):
    scenario = {"turns": [{"match": "go", "steps": [{"text": "ok"}]}]}
    hooks = {
        "onTurn": [say({"annotations": {"keep": 1, "a": "x" * 10000}})],
        "onStop": [say({"annotations": {"b": "y" * 10000}})],
    }
    m, sid = await start(world, scenario, hooks)
    await m.router(sid).submit_turn("go")
    done = await wait_turn(m, sid)
    assert done.annotations == {"keep": 1, "a": "x" * 10000}
    errors = [e for e in events(world, sid, "error") if e["kind"] == "annotations_too_large"]
    assert len(errors) == 1 and errors[0]["turn_id"] == done.turn_id


async def test_on_stop_annotations_merged_on_block_and_defer_legs_defer(world: World):
    task = {"id": "bg1", "command": "sleep"}
    scenario = {
        "turns": [
            {
                "match": "go",
                "steps": [
                    {"text": "started"},
                    {"background": {"after": 1.0, "text": "finished", "background_tasks": []}},
                ],
                "stop": {"background_tasks": [task]},
            }
        ]
    }
    run = (
        'f="$CCW_STATE_DIR/seen"; if [ -f "$f" ]; '
        "then printf '%s' '{\"annotations\":{\"leg\":2}}'; "
        "else touch \"$f\"; printf '%s' "
        '\'{"decision":"defer","annotations":{"leg":1,"d":true}}\'; fi'
    )
    m, sid = await start(
        world, scenario, {"onStop": [{"type": "command", "run": run, "blocking": True}]}
    )
    await m.router(sid).submit_turn("go")
    done = await wait_turn(m, sid)
    assert events(world, sid, "turn_deferred")
    assert done.annotations == {"leg": 2, "d": True}
    (event,) = events(world, sid, "turn_completed")
    assert event["record"]["annotations"] == {"leg": 2, "d": True}


async def test_on_turn_annotations_on_record(world: World):
    scenario = {"turns": [{"match": "go", "steps": [{"text": "ok"}]}]}
    m, sid = await start(world, scenario, {"onTurn": [say({"annotations": {"pending": ["a"]}})]})
    record = await m.router(sid).submit_turn("go")
    assert record.annotations == {"pending": ["a"]}
    done = await wait_turn(m, sid)
    assert m.open_turns(sid).get(record.turn_id).annotations == {"pending": ["a"]}
    assert done.annotations == {"pending": ["a"]}


async def test_on_stop_annotations_in_callback(world: World):
    scenario = {"turns": [{"match": "go", "steps": [{"text": "ok"}]}]}
    m, sid = await start(world, scenario, {"onStop": [say({"annotations": {"stop": 1}})]})
    await m.router(sid).submit_turn("go", callback_url=f"{world.url}/cb")
    await wait_turn(m, sid)
    await world.until(lambda: world.callbacks)
    assert world.callbacks[0]["data"]["record"]["annotations"] == {"stop": 1}
    (event,) = events(world, sid, "turn_completed")
    assert event["record"]["annotations"] == {"stop": 1}


async def test_on_turn_complete_annotations_persisted(world: World):
    scenario = {"turns": [{"match": "go", "steps": [{"text": "ok"}]}]}
    m, sid = await start(world, scenario, {"onTurnComplete": [say({"annotations": {"c": 1}})]})
    record = await m.router(sid).submit_turn("go", callback_url=f"{world.url}/cb")
    await wait_turn(m, sid)
    await m.router(sid).drain(5)
    await world.until(lambda: world.callbacks)
    assert world.callbacks[0]["data"]["record"]["annotations"] == {}
    assert m.open_turns(sid).get(record.turn_id).annotations == {"c": 1}
    assert TurnStore(m.open_turns(sid).path).get(record.turn_id).annotations == {"c": 1}


async def test_non_turn_event_annotations_ignored(world: World):
    scenario = {"turns": [{"match": "go", "steps": [{"text": "ok"}]}]}
    hooks = {
        "onAgentResponse": [say({"annotations": {"x": 1}})],
        "onCreate": [say({"annotations": {"y": 1}})],
    }
    m, sid = await start(world, scenario, hooks)
    record = await m.router(sid).submit_turn("go")
    done = await wait_turn(m, sid)
    await m.router(sid).drain(5)
    assert done.annotations == {}
    assert m.open_turns(sid).get(record.turn_id).annotations == {}
