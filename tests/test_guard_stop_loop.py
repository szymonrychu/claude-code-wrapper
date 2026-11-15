"""Runaway guard: the per-turn cap on Stop-hook continuations (guard.max_stop_continuations)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from conftest import World, wait_turn
from test_stop_gate import Gate, of_type, start, turn, types

from claude_code_wrapper.guard import STOP_LOOP_REASON

PLUGIN_LOOP = {
    "hooks": {
        "Stop": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            'printf \'%s\' \'{"decision":"block","reason":"plugin loop"}\''
                        ),
                    }
                ],
            }
        ]
    }
}


def blocking_gate(world: World, *calls: int) -> Gate:
    gate = Gate(world.tmp)
    for n in calls:
        gate.reply(n, {"decision": "block", "reason": "keep going"})
    return gate


def tripped(router: Any, record: Any, count: int = 3) -> list[dict[str, Any]]:
    return [
        {
            "kind": "stop_loop",
            "turn_id": record.turn_id,
            "count": count,
            "limit": 3,
            "claude_session_id": router.session.spec.claude_session_id,
            "agent_id": None,
            "tool_name": None,
        }
    ]


async def test_stop_loop_capped_at_limit(world: World):
    gate = blocking_gate(world, *range(1, 11))  # the fake's own cap (8) is above the guard's
    cfg = world.cfg(
        turn([{"text": "first"}]),
        hooks={"onStop": [gate.action()]},
        guard={"max_stop_continuations": 3},
    )
    m, sid = await start(world, cfg)
    router = m.router(sid)
    record = await router.submit_turn("go")
    done = await wait_turn(m, sid)
    assert done.turn_id == record.turn_id and done.state == "completed"
    assert types(world, sid, "turn_continued", "guard_tripped", "turn_completed") == [
        "turn_continued"
    ] * 3 + ["guard_tripped", "turn_completed"]
    assert of_type(world, sid, "guard_tripped") == tripped(router, record)
    assert len(Gate.calls(world, sid)) == 3  # the 4th Stop never fired the wrapper onStop


async def test_stop_loop_not_reset_by_tool_calls(world: World):
    gate = blocking_gate(world, *range(1, 11))
    scenario = turn(
        [{"text": "first"}],
        on_block=[
            {"tool": "Bash", "input": {"command": "false"}, "fail": True},
            {"text": "again"},
        ],
    )
    cfg = world.cfg(
        scenario, hooks={"onStop": [gate.action()]}, guard={"max_stop_continuations": 3}
    )
    m, sid = await start(world, cfg)
    router = m.router(sid)
    record = await router.submit_turn("go")
    done = await wait_turn(m, sid)
    assert done.turn_id == record.turn_id
    assert types(world, sid, "turn_continued", "guard_tripped", "turn_completed") == [
        "turn_continued"
    ] * 3 + ["guard_tripped", "turn_completed"]
    assert of_type(world, sid, "guard_tripped") == tripped(router, record)


async def test_stop_loop_capped_without_wrapper_on_stop_hook(world: World):
    cfg = world.cfg(
        turn([{"text": "first"}]),
        claude={"settings": PLUGIN_LOOP},
        guard={"max_stop_continuations": 3},
    )
    m, sid = await start(world, cfg)
    router = m.router(sid)
    record = await router.submit_turn("go")
    done = await wait_turn(m, sid)
    assert done.turn_id == record.turn_id and done.state == "completed"
    # The client turn completes on the first Stop (the wrapper cannot see the plugin block);
    # each continued leg then completes as an agent-origin turn until the guard trips.
    await world.until(lambda: of_type(world, sid, "guard_tripped"))
    assert types(world, sid, "turn_continued") == []
    assert of_type(world, sid, "guard_tripped") == tripped(router, record)
    await world.until(lambda: len(of_type(world, sid, "turn_completed")) == 4)
    await asyncio.sleep(0.5)  # continue:false ended the loop: no further legs
    completed = of_type(world, sid, "turn_completed")
    assert [c["record"]["origin"] for c in completed] == ["client"] + ["agent"] * 3


async def test_stop_loop_new_turn_resets(world: World):
    # turn 1: calls 1, 2 block, 3 allows; turn 2: calls 4, 5 block, 6 allows
    gate = blocking_gate(world, 1, 2, 4, 5)
    cfg = world.cfg(
        turn([{"text": "first"}]),
        hooks={"onStop": [gate.action()]},
        guard={"max_stop_continuations": 3},
    )
    m, sid = await start(world, cfg)
    router = m.router(sid)
    first = await router.submit_turn("go")
    await wait_turn(m, sid)
    second = await router.submit_turn("go")
    await world.until(lambda: len(of_type(world, sid, "turn_completed")) == 2)
    completed = of_type(world, sid, "turn_completed")
    assert [c["record"]["turn_id"] for c in completed] == [first.turn_id, second.turn_id]
    assert of_type(world, sid, "guard_tripped") == []
    assert len(of_type(world, sid, "turn_continued")) == 4


async def test_stop_loop_off_by_default(world: World):
    gate = blocking_gate(world, *range(1, 9))  # 8 blocks (the fake's cap), the 9th allows
    m, sid = await start(
        world, world.cfg(turn([{"text": "first"}]), hooks={"onStop": [gate.action()]})
    )
    router = m.router(sid)
    await router.submit_turn("go")
    await wait_turn(m, sid)
    assert len(of_type(world, sid, "turn_continued")) == 8
    assert of_type(world, sid, "guard_tripped") == []


async def test_stop_loop_fallback_escapes_when_claude_ignores_continue_false(
    world: World, monkeypatch
):
    gate = blocking_gate(world, *range(1, 11))
    cfg = world.cfg(
        turn([{"text": "first"}]),
        hooks={"onStop": [gate.action()]},
        guard={"max_stop_continuations": 3},
    )
    m, sid = await start(world, cfg)
    router = m.router(sid)
    await router.submit_turn("go")
    await wait_turn(m, sid)
    assert router.guard.loop_tripped is True
    escapes: list[int] = []

    async def fake_escape() -> None:
        escapes.append(1)

    monkeypatch.setattr(router.session, "send_escape", fake_escape)
    before = types(world, sid, "turn_started", "turn_completed")
    payload = {
        "session_id": router.session.spec.claude_session_id,
        "stop_hook_active": True,
        "last_assistant_message": "x",
        "background_tasks": [],
        "session_crons": [],
    }
    resp = await router.handle_claude_hook("Stop", payload)
    out = json.loads(resp["stdout"])
    assert out == {"continue": False, "stopReason": STOP_LOOP_REASON.format(limit=3)}
    await world.until(lambda: escapes)
    assert escapes == [1]
    assert types(world, sid, "turn_started", "turn_completed") == before
    assert len(Gate.calls(world, sid)) == 3
    await router.handle_claude_hook("Stop", {**payload, "stop_hook_active": False})
    assert router.guard.loop_tripped is False


async def test_guard_trip_wins_over_pressure_block(tmp_path):
    from types import SimpleNamespace

    from test_pressure_injection import policy, status
    from test_router import CLAUDE_SID, SID, Harness

    from claude_code_wrapper.pressure import PressureState
    from claude_code_wrapper.timeutil import now_iso
    from claude_code_wrapper.turns import TurnRecord

    h = Harness(tmp_path)
    h.cfg.pressure.enabled = True
    h.cfg.guard.max_stop_continuations = 2
    h.router.guard.cfg.max_stop_continuations = 2
    h.router.pressure = PressureState(h.cfg.pressure)
    h.router.pressure.put(policy())
    status(h, 170_000)  # L3: three pressure blocks allowed
    record = TurnRecord(turn_id="turn-1", session_id=SID, state="running", started_at=now_iso())
    h.session.turn = record
    h.session.stop_target = lambda payload, marker=None: record  # type: ignore[attr-defined]
    h.session.spec = SimpleNamespace(resume=False, claude_session_id=CLAUDE_SID)

    def reply(r: dict[str, Any]) -> dict[str, Any]:
        return json.loads(r["stdout"])

    first = await h.router.handle_claude_hook("Stop", {"session_id": CLAUDE_SID})
    assert reply(first)["decision"] == "block"
    second = await h.router.handle_claude_hook(
        "Stop", {"session_id": CLAUDE_SID, "stop_hook_active": True}
    )
    assert reply(second)["decision"] == "block"
    third = await h.router.handle_claude_hook(
        "Stop", {"session_id": CLAUDE_SID, "stop_hook_active": True}
    )
    assert reply(third)["continue"] is False
    assert [e.data["count"] for e in h.events("pressure_stop_blocked")] == [1, 2]
    assert [e.data["kind"] for e in h.events("guard_tripped")] == ["stop_loop"]
    await h.router.close()
