"""Same-turn lifecycle of a slash command queued during a turn (design D4 steps 2, 4-7).

The command runs at the turn's Stop, after the relay answered claude, and the agent resumes in the
same turn with the continue prompt, the results line and the same turn marker. Interrupt, crash
and `turn_max_seconds` end the turn without a continuation.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from typing import Any

import pytest
from conftest import World, jsonl, turns_of
from test_command_runner import commands_cfg, finished, inputs, of_type
from test_stop_gate import TASK, Gate

from claude_code_wrapper.manager import CreateSessionRequest, SessionManager
from claude_code_wrapper.session import PROJECT_DIR_NAME

CONTINUE = "Resume after commands"
GO = {"match": "^go", "steps": [{"sleep": 0.8}, {"text": "part one"}]}
RESUMED = {"match": "^Resume after commands", "steps": [{"text": "part two"}]}
RESTART = "Restarted mid-turn, continue"
RESTARTED = {"match": "^Restarted", "steps": [{"text": "after restart"}]}
HANG = {"reload-skills": {"hang": True}}


async def start(
    world: World,
    turns: list[dict] | None = None,
    *,
    commands: dict | None = None,
    sessions: dict | None = None,
    hooks: dict | None = None,
    timeouts: dict | None = None,
    **sections: Any,
) -> tuple[SessionManager, str, Any]:
    scenario = {"turns": turns if turns is not None else [GO, RESUMED], "commands": commands or {}}
    cmds = commands_cfg(
        continue_prompt=CONTINUE, **({"timeout_seconds": timeouts} if timeouts else {})
    )
    cfg = world.cfg(
        scenario,
        sessions={"commands": cmds, **(sessions or {})},
        hooks=hooks or {},
        **sections,
    )
    m = world.manager(cfg)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    return m, sid, m.router(sid)


def types(world: World, sid: str, *wanted: str) -> list[str]:
    return [e["type"] for e in world.events(sid) if e["type"] in wanted]


def typed(world: World, sid: str) -> list[str]:
    return [i["text"] for i in inputs(world, sid)]


def user_strings(world: World, sid: str) -> list[dict]:
    claude_sid = world.meta(sid)["claude_session_id"]
    path = world.session_dir(sid) / "config" / "projects" / PROJECT_DIR_NAME
    return [
        line
        for line in jsonl(path / f"{claude_sid}.jsonl")
        if line.get("type") == "user" and isinstance(line["message"].get("content"), str)
    ]


def record_writes(router: Any) -> list[bytes]:
    sent: list[bytes] = []
    pty = router.session.pty
    write = pty.write

    async def recording(data: bytes) -> None:
        sent.append(bytes(data))
        await write(data)

    pty.write = recording
    return sent


ORDER = (
    "turn_started",
    "turn_deferred",
    "command_started",
    "compacted",
    "command_completed",
    "turn_resumed",
    "turn_completed",
)


async def test_command_runs_at_stop_and_turn_resumes(world):
    m, sid, router = await start(world)
    turn = await router.submit_turn("go")
    rec = router.request_command("compact", origin="client")
    assert rec.turn_id == turn.turn_id and rec.state == "queued"
    done = await world.wait_turn(m, sid)
    assert done.turn_id == turn.turn_id and done.final_text == "part two"
    assert (rec.state, rec.result) == ("completed", "ok")
    assert [t.turn_id for t in turns_of(m, sid)] == [turn.turn_id]
    assert types(world, sid, *ORDER) == list(ORDER)
    # The record stayed the running turn: busy from the submit until the completion's ready.
    evs = world.events(sid)
    begin = next(i for i, e in enumerate(evs) if e["type"] == "turn_started")
    end = next(i for i, e in enumerate(evs) if e["type"] == "turn_completed")
    states = [e["data"]["state"] for e in evs[begin:end] if e["type"] == "session_state"]
    assert states == ["ready"]
    assert typed(world, sid)[1] == "/compact"
    assert typed(world, sid)[2].startswith(CONTINUE)
    assert {i["state"] for i in inputs(world, sid)} == {"idle"}
    await world.until(lambda: router.session.state == "ready")


async def test_runner_starts_after_relay_answer(world):
    m, sid, router = await start(world)
    order: list[tuple[str, Any]] = []
    relay, send = router.handle_claude_hook, router.session.send_command

    async def answered(event: str, payload: Any) -> Any:
        out = await relay(event, payload)
        if event == "Stop":
            order.append(("stop_answered", dict(out)))
        return out

    async def typing(line: str) -> None:
        order.append(("typed", line))
        await send(line)

    router.handle_claude_hook = answered
    router.session.send_command = typing
    await router.submit_turn("go")
    rec = router.request_command("reload-plugins", origin="agent")
    await world.wait_turn(m, sid)
    assert rec.state == "completed"
    allow = {"stdout": "", "stderr": "", "exit_code": 0}
    assert order == [
        ("stop_answered", allow),
        ("typed", "/reload-plugins"),
        ("stop_answered", allow),
    ]


async def test_onstop_block_keeps_command_queued(world):
    gate = Gate(world.tmp)
    gate.reply(1, {"decision": "block", "reason": "check the tests"})
    m, sid, router = await start(world, hooks={"onStop": [gate.action()]})
    turn = await router.submit_turn("go")
    rec = router.request_command("compact", origin="client")
    await world.until(lambda: of_type(world, sid, "turn_continued"))
    done = await world.wait_turn(m, sid)
    assert done.turn_id == turn.turn_id and done.final_text == "part two"
    assert rec.state == "completed"
    seen = types(world, sid, "turn_continued", "turn_deferred", "command_started", "turn_completed")
    assert seen == ["turn_continued", "turn_deferred", "command_started", "turn_completed"]
    (deferred,) = of_type(world, sid, "turn_deferred")
    assert deferred["reason"] == "command" and deferred["commands"] == [rec.id]
    # Stop 1 blocked, Stop 2 (after the block) let the command run, Stop 3 completed.
    assert len(Gate.calls(world, sid)) == 3


async def test_background_defer_keeps_command_queued(world):
    go = {
        "match": "^go",
        "steps": [
            {"sleep": 0.8},
            {"text": "started tests"},
            {"background": {"after": 1.5, "text": "tests passed", "background_tasks": []}},
        ],
        "stop": {"background_tasks": [TASK]},
    }
    m, sid, router = await start(
        world, [go, RESUMED], sessions={"defer_stop_on_background_tasks": True}
    )
    turn = await router.submit_turn("go")
    rec = router.request_command("compact", origin="client")
    await world.until(lambda: of_type(world, sid, "turn_deferred"))
    assert rec.state == "queued"
    await asyncio.sleep(0.6)  # the runner did not start at the deferred Stop
    assert rec.state == "queued" and "/compact" not in typed(world, sid)
    done = await world.wait_turn(m, sid)
    assert done.turn_id == turn.turn_id and done.final_text == "part two"
    assert rec.state == "completed"
    assert of_type(world, sid, "turn_deferred") == [
        {
            "turn_id": turn.turn_id,
            "reason": "background_tasks",
            "background_tasks": [TASK],
            "commands": [rec.id],
        },
        {
            "turn_id": turn.turn_id,
            "reason": "command",
            "background_tasks": [],
            "commands": [rec.id],
        },
    ]
    assert len(of_type(world, sid, "turn_completed")) == 1


async def test_continue_prompt_lists_results_with_same_marker(world):
    skipped = "MCP servers changed. Run /reload-plugins --force to apply."
    m, sid, router = await start(
        world,
        commands={"reload-plugins": {"stdout": skipped}, "context": {"hang": True}},
        timeouts={"compact": 5, "context": 0.6, "default": 5},
    )
    turn = await router.submit_turn("go")
    recs = [
        router.request_command("compact", origin="agent"),
        router.request_command("reload-plugins", origin="agent"),
        router.request_command("context", origin="client"),
    ]
    done = await world.wait_turn(m, sid)
    assert done.turn_id == turn.turn_id
    tid = turn.turn_id
    results = (
        "Command results: compact completed ok; reload-plugins completed skipped_cache; "
        "context failed timeout"
    )
    prompts = [line["message"]["content"] for line in user_strings(world, sid)]
    assert [p for p in prompts if p.startswith(CONTINUE)] == [
        f"{CONTINUE}\n\n{results}\n\n<!-- ccw turn: {tid} -->"
    ]
    assert [p for p in prompts if p.startswith("go")] == [f"go\n\n<!-- ccw turn: {tid} -->"]
    assert of_type(world, sid, "turn_resumed") == [
        {"turn_id": tid, "cause": "command", "commands": [r.id for r in recs]}
    ]


async def test_stop_after_compact_summary_completes_turn(world):
    m, sid, router = await start(world, [GO, RESUMED, {"match": "^first", "steps": []}])
    first = await router.submit_turn("first")
    await world.until(lambda: first.state == "completed")
    await world.until(lambda: router.session.state == "ready")
    second = await router.submit_turn("go")
    router.request_command("compact", origin="agent")
    await world.until(lambda: second.state != "running")
    assert second.state == "completed" and second.final_text == "part two"
    # The summary quotes the first turn's marker before this one's.
    (summary,) = [line for line in user_strings(world, sid) if line.get("isCompactSummary")]
    text = summary["message"]["content"]
    assert text.index(first.turn_id) < text.index(second.turn_id)
    completed = [e["record"]["turn_id"] for e in of_type(world, sid, "turn_completed")]
    assert completed == [first.turn_id, second.turn_id]


async def test_interrupt_cancels_queued_and_running(world):
    m, sid, router = await start(world, commands=HANG, sessions={"interrupt_settle_seconds": 5})
    sent = record_writes(router)
    turn = await router.submit_turn("go")
    running = router.request_command("reload-skills", origin="client")
    queued = router.request_command("context", origin="client")
    await world.until(lambda: running.state == "running")
    began = time.monotonic()
    assert await router.interrupt() == turn.turn_id
    assert time.monotonic() - began < 3  # no interrupt settle wait: no claude turn is running
    assert turn.state == "interrupted"
    assert (running.state, running.error) == ("failed", "interrupted")
    assert queued.state == "cancelled" and queued.started_at is None
    assert sent.count(b"\x1b") == 1
    await world.until(lambda: router.session.state == "ready")
    await asyncio.sleep(0.5)
    assert not [t for t in typed(world, sid) if t.startswith(CONTINUE)]
    assert of_type(world, sid, "turn_resumed") == [] and of_type(world, sid, "turn_completed") == []
    (failed,) = of_type(world, sid, "turn_failed")
    assert failed["record"]["state"] == "interrupted"


async def test_interrupt_before_stop_cancels_queued(world):
    m, sid, router = await start(world, sessions={"interrupt_settle_seconds": 1})
    turn = await router.submit_turn("go")
    queued = router.request_command("compact", origin="client")
    assert await router.interrupt() == turn.turn_id
    await finished(world, queued)
    assert queued.state == "cancelled" and turn.state == "interrupted"
    assert "/compact" not in typed(world, sid)


@pytest.mark.parametrize("prompt", [RESTART, ""])
async def test_crash_mid_command_follows_k12(world, prompt):
    m, sid, router = await start(
        world,
        [GO, RESUMED, RESTARTED],
        commands=HANG,
        sessions={"restart_continue_prompt": prompt},
    )
    turn = await router.submit_turn("go")
    running = router.request_command("reload-skills", origin="client")
    queued = router.request_command("context", origin="client")
    await world.until(lambda: running.state == "running")
    os.kill(router.session.pty.pid, signal.SIGKILL)
    await finished(world, running)
    await finished(world, queued)
    assert (running.state, running.error) == ("failed", "claude_exited")
    assert queued.state == "cancelled"
    if prompt:
        done = await world.wait_turn(m, sid)
        assert done.turn_id == turn.turn_id and done.final_text == "after restart"
        assert of_type(world, sid, "turn_resumed") == [
            {"turn_id": turn.turn_id, "restart": 1, "cause": "restart"}
        ]
    else:
        await world.until(lambda: turn.state != "running")
        assert (turn.state, turn.error) == ("failed", "claude_exited")
        assert of_type(world, sid, "turn_resumed") == []
    await world.until(lambda: m.get(sid)["state"] == "ready")
    assert not [t for t in typed(world, sid) if t.startswith(CONTINUE)]


async def test_turn_max_seconds_counts_through_command(world):
    go = {"match": "^go", "steps": [{"sleep": 0.3}, {"text": "part one"}]}
    m, sid, router = await start(
        world, [go, RESUMED], commands=HANG, limits={"turn_max_seconds": 2}
    )
    sent = record_writes(router)
    turn = await router.submit_turn("go")
    running = router.request_command("reload-skills", origin="client")
    queued = router.request_command("context", origin="client")
    await world.until(lambda: running.state == "running")
    await world.until(lambda: turn.state != "running")
    assert turn.state == "timeout"
    await finished(world, running)
    await finished(world, queued)
    assert (running.state, running.error) == ("failed", "timeout")
    assert queued.state == "cancelled"
    assert sent.count(b"\x1b") == 1
    await world.until(lambda: router.session.state == "ready")
    await asyncio.sleep(0.5)
    assert not [t for t in typed(world, sid) if t.startswith(CONTINUE)]
    (failed,) = of_type(world, sid, "turn_failed")
    assert failed["record"]["state"] == "timeout"


async def test_usage_accumulates_across_command(world):
    m, sid, router = await start(world)
    turn = await router.submit_turn("go")
    router.request_command("compact", origin="client")
    done = await world.wait_turn(m, sid)
    assert done.turn_id == turn.turn_id
    # "part one" before the command, "part two" after it: one record sums both legs.
    assert done.usage is not None and done.usage["output_tokens"] == 4
    assert types(world, sid, "turn_started", "turn_completed") == ["turn_started", "turn_completed"]


async def test_deferred_reason_and_resumed_cause(world):
    # onStop defer: reason "hook".
    gate = Gate(world.tmp)
    gate.reply(1, {"decision": "defer"})
    go = {
        "match": "^go",
        "steps": [
            {"text": "a"},
            {"background": {"after": 0.8, "text": "bg done", "background_tasks": []}},
        ],
    }
    m, sid, router = await start(world, [go, RESUMED], hooks={"onStop": [gate.action()]})
    turn = await router.submit_turn("go")
    await world.wait_turn(m, sid)
    assert of_type(world, sid, "turn_deferred") == [
        {"turn_id": turn.turn_id, "reason": "hook", "background_tasks": [], "commands": []}
    ]
    # A queued command: reason "command" and cause "command".
    m, sid, router = await start(world)
    turn = await router.submit_turn("go")
    rec = router.request_command("compact", origin="client")
    await world.wait_turn(m, sid)
    assert of_type(world, sid, "turn_deferred") == [
        {"turn_id": turn.turn_id, "reason": "command", "background_tasks": [], "commands": [rec.id]}
    ]
    assert of_type(world, sid, "turn_resumed") == [
        {"turn_id": turn.turn_id, "cause": "command", "commands": [rec.id]}
    ]
    # The K12 restart resume: cause "restart".
    crash = {"match": "^go", "steps": [{"text": "part one"}, {"exit": 3}]}
    m, sid, router = await start(
        world, [crash, RESTARTED], sessions={"restart_continue_prompt": RESTART}
    )
    turn = await router.submit_turn("go")
    await world.wait_turn(m, sid)
    assert of_type(world, sid, "turn_resumed") == [
        {"turn_id": turn.turn_id, "restart": 1, "cause": "restart"}
    ]
