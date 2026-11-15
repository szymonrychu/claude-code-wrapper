"""Runaway guard: the per-claude-session cap on Agent/Task launches (guard.max_agent_spawns)."""

from __future__ import annotations

import json

from conftest import World, wait_turn
from test_stop_gate import of_type, start, turn

from claude_code_wrapper.guard import AGENT_SPAWN_DENY_REASON


def spawn_steps(tool: str, n: int) -> list[dict]:
    return [{"tool": tool, "input": {"description": "d", "prompt": "p"}} for _ in range(n)]


async def run_turn(world: World, steps: list[dict], **guard: int):
    cfg = world.cfg(turn(steps), guard=guard) if guard else world.cfg(turn(steps))
    m, sid = await start(world, cfg)
    router = m.router(sid)
    record = await router.submit_turn("go")
    await wait_turn(m, sid)
    return m, sid, router, record


async def test_agent_spawn_denied_past_cap(world: World):
    steps = spawn_steps("Agent", 4)
    m, sid, router, record = await run_turn(world, steps, max_agent_spawns=2)
    denied = of_type(world, sid, "tool_denied")
    assert [d["reason"] for d in denied] == [AGENT_SPAWN_DENY_REASON.format(limit=2)] * 2
    assert len(of_type(world, sid, "tool_call")) == 2
    assert of_type(world, sid, "guard_tripped") == [
        {
            "kind": "agent_spawns",
            "turn_id": record.turn_id,
            "count": 2,
            "limit": 2,
            "claude_session_id": router.session.spec.claude_session_id,
            "agent_id": None,
            "tool_name": "Agent",
        }
    ]


async def test_task_alias_counts(world: World):
    m, sid, router, record = await run_turn(world, spawn_steps("Task", 3), max_agent_spawns=2)
    denied = of_type(world, sid, "tool_denied")
    assert len(denied) == 1 and denied[0]["tool_name"] == "Task"
    (trip,) = of_type(world, sid, "guard_tripped")
    assert trip["tool_name"] == "Task" and trip["count"] == 2


async def test_other_tools_untouched(world: World):
    steps = spawn_steps("Agent", 1) + [{"tool": "Bash", "input": {"command": "true"}}] * 5
    m, sid, router, record = await run_turn(world, steps, max_agent_spawns=1)
    assert of_type(world, sid, "tool_denied") == []
    assert of_type(world, sid, "guard_tripped") == []


async def _hook(router, sid: str, use_id: str, **extra):
    return await router.handle_claude_hook(
        "PreToolUse",
        {
            "session_id": sid,
            "tool_name": "Agent",
            "tool_input": {},
            "tool_use_id": use_id,
            **extra,
        },
    )


async def test_spawn_count_per_claude_session_id(world: World):
    m, sid = await start(world, world.cfg(turn([]), guard={"max_agent_spawns": 1}))
    router = m.router(sid)
    assert (await _hook(router, "c1", "u1"))["stdout"] == ""
    out = json.loads((await _hook(router, "c1", "u2"))["stdout"])["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert (await _hook(router, "c2", "u3"))["stdout"] == ""


async def test_nested_spawn_counts(world: World):
    m, sid = await start(world, world.cfg(turn([]), guard={"max_agent_spawns": 1}))
    router = m.router(sid)
    assert (await _hook(router, "c1", "u1"))["stdout"] == ""
    out = json.loads((await _hook(router, "c1", "u2", agent_id="a1"))["stdout"])
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    (trip,) = of_type(world, sid, "guard_tripped")
    assert trip["agent_id"] == "a1" and trip["claude_session_id"] == "c1"


async def test_missing_session_id_counts_under_empty(world: World):
    m, sid = await start(world, world.cfg(turn([]), guard={"max_agent_spawns": 1}))
    router = m.router(sid)
    payload = {"tool_name": "Agent", "tool_input": {}, "tool_use_id": "u1"}
    assert (await router.handle_claude_hook("PreToolUse", payload))["stdout"] == ""
    out = await router.handle_claude_hook("PreToolUse", payload)
    assert "deny" in out["stdout"]


async def test_spawn_cap_off_by_default(world: World):
    m, sid, router, record = await run_turn(world, spawn_steps("Agent", 10))
    assert of_type(world, sid, "tool_denied") == []
    assert of_type(world, sid, "guard_tripped") == []
