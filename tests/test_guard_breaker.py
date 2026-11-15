"""Runaway guard: the repeated-failure breaker (guard.tool_failure_repeats)."""

from __future__ import annotations

from conftest import World, wait_turn
from test_stop_gate import of_type, start, turn

from claude_code_wrapper.guard import BREAKER_DENY_REASON
from claude_code_wrapper.hooks import HookOutcome

REASON = BREAKER_DENY_REASON.format(count=3)


def bash(command: str, **kw) -> dict:
    return {"tool": "Bash", "input": {"command": command}, **kw}


async def run_turn(world: World, steps: list[dict], **guard: int):
    cfg = world.cfg(turn(steps), guard=guard)
    m, sid = await start(world, cfg)
    router = m.router(sid)
    record = await router.submit_turn("go")
    await wait_turn(m, sid)
    return m, sid, router, record


async def test_breaker_denies_fourth_identical_call(world: World):
    steps = [bash("false", fail=True) for _ in range(4)]
    m, sid, router, record = await run_turn(world, steps, tool_failure_repeats=3)
    denied = of_type(world, sid, "tool_denied")
    assert [d["reason"] for d in denied] == [REASON]
    assert [e["kind"] for e in of_type(world, sid, "guard_tripped")] == ["tool_breaker"]
    assert of_type(world, sid, "guard_tripped")[0] == {
        "kind": "tool_breaker",
        "turn_id": record.turn_id,
        "count": 3,
        "limit": 3,
        "claude_session_id": router.session.spec.claude_session_id,
        "agent_id": None,
        "tool_name": "Bash",
    }


async def _pre(router, use_id: str, tool_input: dict, **extra):
    return await router.handle_claude_hook(
        "PreToolUse",
        {
            "session_id": "c1",
            "tool_name": "Edit",
            "tool_input": tool_input,
            "tool_use_id": use_id,
            **extra,
        },
    )


async def _fail(router, use_id: str, tool_input: dict, **extra):
    return await router.handle_claude_hook(
        "PostToolUseFailure",
        {
            "session_id": "c1",
            "tool_name": "Edit",
            "tool_input": tool_input,
            "tool_use_id": use_id,
            "error": "boom",
            **extra,
        },
    )


async def test_breaker_key_ignores_key_order_and_counts_denial_once(world: World):
    m, sid = await start(world, world.cfg(turn([]), guard={"tool_failure_repeats": 3}))
    router = m.router(sid)
    await _fail(router, "u1", {"b": 2, "a": 1})
    await _fail(router, "u2", {"a": 1, "b": 2})
    await _fail(router, "u3", {"a": 1, "b": 2})
    assert router.guard.streak("main") == 3
    out = await _pre(router, "u4", {"a": 1, "b": 2})
    assert "permissionDecision" in out["stdout"] and REASON in out["stdout"]
    assert router.guard.streak("main") == 4
    await _fail(router, "u4", {"a": 1, "b": 2})
    assert router.guard.streak("main") == 4


async def test_breaker_different_input_allowed(world: World):
    steps = [bash("false", fail=True)] * 3 + [bash("true")]
    m, sid, router, record = await run_turn(world, steps, tool_failure_repeats=3)
    assert of_type(world, sid, "tool_denied") == []
    assert of_type(world, sid, "guard_tripped") == []


async def test_breaker_success_resets(world: World):
    steps = [bash("x", fail=True)] * 2 + [bash("x")] + [bash("x", fail=True)] * 3
    m, sid, router, record = await run_turn(world, steps, tool_failure_repeats=3)
    assert of_type(world, sid, "tool_denied") == []


async def test_breaker_per_agent_keys(world: World):
    m, sid = await start(world, world.cfg(turn([]), guard={"tool_failure_repeats": 3}))
    router = m.router(sid)
    for i in range(3):
        await _fail(router, f"a{i}", {"x": 1}, agent_id="a1")
    denied = await _pre(router, "p1", {"x": 1}, agent_id="a1")
    assert REASON in denied["stdout"]
    assert (await _pre(router, "p2", {"x": 1}))["stdout"] == ""


async def test_hook_denials_count_and_breaker_skips_hook(world: World):
    m, sid = await start(world, world.cfg(turn([]), guard={"tool_failure_repeats": 3}))
    router = m.router(sid)
    calls = []

    async def fire(event, data, **kw):
        if event == "onAgentToolCall":
            calls.append(data)
            return HookOutcome(decision="deny", reason="no")
        return HookOutcome()

    router._fire = fire  # type: ignore[method-assign]
    for i in range(3):
        out = await _pre(router, f"h{i}", {"command": "x"}, tool_name="Bash")
        assert "no" in out["stdout"]
    out = await _pre(router, "h3", {"command": "x"}, tool_name="Bash")
    assert len(calls) == 3
    assert REASON in out["stdout"]
    reasons = [d["reason"] for d in of_type(world, sid, "tool_denied")]
    assert reasons[-1] == REASON


async def test_breaker_off_by_default(world: World):
    steps = [bash("false", fail=True) for _ in range(10)]
    m, sid = await start(world, world.cfg(turn(steps)))
    router = m.router(sid)
    await router.submit_turn("go")
    await wait_turn(m, sid)
    assert of_type(world, sid, "tool_denied") == []
