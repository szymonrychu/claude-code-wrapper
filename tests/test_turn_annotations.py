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


def test_record_defaults_empty_and_old_lines_load(tmp_path: Path):
    path = tmp_path / "turns.jsonl"
    old = {"turn_id": "t1", "session_id": "s", "started_at": "2026-01-01T00:00:00Z"}
    path.write_text(json.dumps(old) + "\n")
    store = TurnStore(path)
    assert store.get("t1").annotations == {}
    record = TurnRecord(turn_id="t2", session_id="s", started_at="x", annotations={"a": [1]})
    store.put(record)
    assert TurnStore(path).get("t2").annotations == {"a": [1]}


async def test_merges_from_turn_scoped_events_later_keys_win(world: World):
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


async def test_block_leg_annotations_merge_before_completion(world: World):
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


async def test_oversized_annotations_dropped_with_error_event(world: World):
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
