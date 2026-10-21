"""K12 turn continuity across a mid-turn crash (design D16), against the fake claude.

With `sessions.restart_continue_prompt` set, a claude that exits mid-turn with restarts left
keeps the turn running; the resumed relaunch is sent the continue prompt with the same turn
marker and the turn completes on its Stop. Every other way out fails or ends the turn once.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from conftest import World, jsonl, turns_of, wait_until
from test_session_turns import Harness, harness  # noqa: F401 - the fixture is used by name

from claude_code_wrapper.config import Config, SessionsConfig
from claude_code_wrapper.events import EVENT_TYPES
from claude_code_wrapper.manager import CreateSessionRequest, SessionManager
from claude_code_wrapper.session import PROJECT_DIR_NAME

CONTINUE = "Resume: claude restarted mid-turn, continue the task"
CRASH = {"match": "^go", "steps": [{"text": "part one"}, {"exit": 3}]}
RESUME = {"match": "^Resume", "steps": [{"text": "part two"}]}


def counter(path: Path) -> dict[str, Any]:
    return {"type": "command", "run": f"cat >> {path}; echo >> {path}", "blocking": True}


def of_type(world: World, sid: str, type_: str) -> list[dict[str, Any]]:
    return [e["data"] for e in world.events(sid) if e["type"] == type_]


def transcript(world: World, sid: str, claude_sid: str) -> Path:
    return world.session_dir(sid) / "config" / "projects" / PROJECT_DIR_NAME / f"{claude_sid}.jsonl"


def user_prompts(path: Path) -> list[str]:
    out = []
    for line in jsonl(path):
        content = line.get("message", {}).get("content")
        if line.get("type") == "user" and isinstance(content, str):
            out.append(content)
    return out


def elapsed(record: Any) -> float:
    return (
        dt.datetime.fromisoformat(record.completed_at)
        - dt.datetime.fromisoformat(record.started_at)
    ).total_seconds()


def rewrite_scenario(cfg: Config, scenario: dict[str, Any] | str) -> None:
    """The next fake launch reads this scenario (the running one already loaded its own)."""
    path = Path(cfg.claude.env.extra["FAKE_CLAUDE_SCENARIO"])
    path.write_text(scenario if isinstance(scenario, str) else json.dumps(scenario))


async def start(
    world: World, turns: list[dict], *, sessions: dict | None = None, **sections: Any
) -> tuple[SessionManager, Config, str, str]:
    cfg = world.cfg(
        {"turns": turns},
        sessions={"restart_continue_prompt": CONTINUE, **(sessions or {})},
        **sections,
    )
    m = world.manager(cfg)
    created = await m.create(CreateSessionRequest())
    return m, cfg, created["session_id"], created["claude_session_id"]


def slow_relaunch(cfg: Config, turns: list[dict], seconds: float) -> None:
    rewrite_scenario(cfg, {"boot": {"bypass_dialog": True, "min_seconds": seconds}, "turns": turns})


# ---------------------------------------------------------------- config and vocabulary


def test_restart_continue_prompt_defaults_off() -> None:
    assert SessionsConfig().restart_continue_prompt == ""
    assert Config().sessions.restart_continue_prompt == ""
    assert "turn_resumed" in EVENT_TYPES


# ---------------------------------------------------------------- the continued turn


async def continued_turn(world: World, **sections: Any) -> tuple[SessionManager, str, str, Any]:
    """Crash mid-turn once, resume, and wait for the same turn to complete."""
    m, _, sid, claude_sid = await start(world, [CRASH, RESUME], **sections)
    record = await m.router(sid).submit_turn("go now", callback_url=f"{world.url}/cb")
    await world.until(lambda: record.state != "running")
    return m, sid, claude_sid, record


async def test_crash_mid_turn_continues_same_turn(world: World, tmp_path: Path) -> None:
    completes = tmp_path / "completes.jsonl"
    m, cfg, sid, _ = await start(
        world, [CRASH, RESUME], hooks={"onTurnComplete": [counter(completes)]}
    )
    slow_relaunch(cfg, [CRASH, RESUME], 1.0)
    router = m.router(sid)
    record = await router.submit_turn("go now", callback_url=f"{world.url}/cb")
    tid = record.turn_id

    # While claude is relaunched the turn stays running: no failure, no completion hook.
    await world.until(lambda: m.get(sid)["state"] == "creating")
    assert [t.state for t in turns_of(m, sid)] == ["running"]
    assert record.state == "running" and record.error is None

    await world.until(lambda: [t for t in turns_of(m, sid) if t.state == "completed"])
    [done] = turns_of(m, sid)
    assert done.turn_id == tid and done.state == "completed"
    assert done.final_text == "part two"
    assert of_type(world, sid, "turn_failed") == []
    assert [e["record"]["turn_id"] for e in of_type(world, sid, "turn_completed")] == [tid]
    await world.until(lambda: len(jsonl(completes)) == 1 and world.callbacks)
    assert [c["type"] for c in world.callbacks] == ["turn_completed"]
    assert m.get(sid)["state"] == "ready" and m.get(sid)["restarts"] == 1
    argvs = world.interactive_argvs()
    assert len(argvs) == 2 and "--resume" in argvs[1]
    # The session takes the next turn normally.
    nxt = await router.submit_turn("Resume is not the word here")
    await world.until(lambda: nxt.state == "completed")


async def test_continue_prompt_carries_same_marker(world: World) -> None:
    _, sid, claude_sid, record = await continued_turn(world)
    assert record.state == "completed"
    tid = record.turn_id
    # The original prompt was not re-pasted; the continue prompt carries the same marker.
    prompts = user_prompts(transcript(world, sid, claude_sid))
    assert [p for p in prompts if p.startswith("go now")] == [f"go now\n\n<!-- ccw turn: {tid} -->"]
    assert [p for p in prompts if p.startswith("Resume")] == [
        f"{CONTINUE}\n\n<!-- ccw turn: {tid} -->"
    ]


async def test_turn_resumed_event(world: World) -> None:
    m, sid, _, record = await continued_turn(world)
    assert record.state == "completed"
    assert of_type(world, sid, "turn_resumed") == [
        {"turn_id": record.turn_id, "restart": 1, "cause": "restart"}
    ]
    assert m.get(sid)["restarts"] == 1


async def test_usage_spans_crash(world: World) -> None:
    _, _, _, record = await continued_turn(world)
    assert record.state == "completed" and record.final_text == "part two"
    # Usage spans both claude processes: "part one" before the crash, "part two" after.
    assert record.usage is not None and record.usage["output_tokens"] == 4


# ---------------------------------------------------------------- failing as today, once


async def test_budget_exhausted_fails_turn_once(world: World, tmp_path: Path) -> None:
    completes = tmp_path / "completes.jsonl"
    crash_again = {"match": "^Resume", "steps": [{"text": "part two"}, {"exit": 4}]}
    m, _, sid, _ = await start(
        world,
        [CRASH, crash_again],
        sessions={"max_restarts": 1},
        hooks={"onTurnComplete": [counter(completes)]},
    )
    record = await m.router(sid).submit_turn("go now", callback_url=f"{world.url}/cb")

    await world.until(lambda: record.state != "running")
    assert record.state == "failed" and record.error == "claude_exited"
    assert of_type(world, sid, "turn_resumed") == [
        {"turn_id": record.turn_id, "restart": 1, "cause": "restart"}
    ]
    failed = of_type(world, sid, "turn_failed")
    assert [(e["record"]["turn_id"], e["record"]["error"]) for e in failed] == [
        (record.turn_id, "claude_exited")
    ]
    await world.until(lambda: len(jsonl(completes)) == 1 and world.callbacks)
    assert [c["type"] for c in world.callbacks] == ["turn_failed"]
    assert of_type(world, sid, "turn_completed") == []
    assert [t.error for t in turns_of(m, sid)] == ["claude_exited"]


async def test_off_by_default_fails_as_today(world: World, tmp_path: Path) -> None:
    completes = tmp_path / "completes.jsonl"
    # The option empty (the default) with restarts left: the crash fails the turn at once.
    m, cfg, sid, claude_sid = await start(
        world,
        [CRASH, RESUME],
        sessions={"restart_continue_prompt": ""},
        hooks={"onTurnComplete": [counter(completes)]},
    )
    assert cfg.sessions.restart_continue_prompt == "" and cfg.sessions.max_restarts > 0
    record = await m.router(sid).submit_turn("go now", callback_url=f"{world.url}/cb")

    await world.until(lambda: record.state != "running")
    assert record.state == "failed" and record.error == "claude_exited"
    failed = of_type(world, sid, "turn_failed")
    assert [(e["record"]["turn_id"], e["record"]["error"]) for e in failed] == [
        (record.turn_id, "claude_exited")
    ]
    await world.until(lambda: len(jsonl(completes)) == 1 and world.callbacks)
    assert [c["type"] for c in world.callbacks] == ["turn_failed"]
    # The session still restarts (resumed) and is ready, but nothing is continued.
    await world.until(lambda: m.get(sid)["state"] == "ready" and m.get(sid)["restarts"] == 1)
    assert of_type(world, sid, "turn_resumed") == []
    assert of_type(world, sid, "turn_completed") == []
    assert [t.error for t in turns_of(m, sid)] == ["claude_exited"]
    assert not [p for p in user_prompts(transcript(world, sid, claude_sid)) if "Resume" in p]


async def test_no_restarts_left_fails_at_once(world: World) -> None:
    m, _, sid, _ = await start(world, [CRASH, RESUME], sessions={"max_restarts": 0})
    record = await m.router(sid).submit_turn("go now")
    await world.until(lambda: record.state != "running")
    assert record.error == "claude_exited"
    assert of_type(world, sid, "turn_resumed") == []
    assert len(of_type(world, sid, "turn_failed")) == 1


async def test_relaunch_that_fails_to_boot_fails_turn(world: World) -> None:
    m, cfg, sid, claude_sid = await start(world, [CRASH, RESUME])
    rewrite_scenario(cfg, "not json: every relaunch exits at once")
    record = await m.router(sid).submit_turn("go now")

    await world.until(lambda: record.state != "running")
    assert record.state == "failed" and record.error == "claude_exited"
    await world.until(lambda: m.get(sid)["state"] == "dead")
    assert of_type(world, sid, "turn_resumed") == []
    assert len(of_type(world, sid, "turn_failed")) == 1
    assert not [p for p in user_prompts(transcript(world, sid, claude_sid)) if "Resume" in p]


async def test_fresh_relaunch_cannot_continue(harness: Harness) -> None:  # noqa: F811
    session = await harness.start(
        {"turns": [CRASH, RESUME]}, sessions={"restart_continue_prompt": CONTINUE}
    )
    real = Path(session.transcript_path)
    # No transcript where the session looks: the relaunch is fresh, nothing to continue.
    session.transcript_path = str(harness.tmp_path / "missing.jsonl")
    record = await session.submit("go now")

    await wait_until(lambda: harness.listener.restarts, timeout=15)
    assert harness.listener.restarts == [(1, False)]
    assert record.state == "failed" and record.error == "claude_exited"
    assert [r.error for r in harness.listener.ended] == ["claude_exited"]
    assert session.current_turn is None and session.state == "ready"
    assert "Resume" not in real.read_text()


# ---------------------------------------------------------------- limits and ends during recovery


async def test_turn_max_seconds_spans_crash(world: World) -> None:
    resume_slow = {"match": "^Resume", "steps": [{"sleep": 30}]}
    m, cfg, sid, _ = await start(
        world,
        [CRASH, resume_slow],
        sessions={"turn_stall_seconds": 1.0, "interrupt_settle_seconds": 1.0},
        limits={"turn_max_seconds": 3.5},
    )
    slow_relaunch(cfg, [CRASH, resume_slow], 2.0)
    record = await m.router(sid).submit_turn("go now")

    await world.until(lambda: record.state != "running", timeout=20)
    assert record.state == "timeout"
    # From the original submit, not from the resume (which came about 2.5s later).
    assert 3.4 <= elapsed(record) < 5.5
    # The stall watch kept running through the relaunch: the quiet boot was reported.
    assert record.turn_id in [e["turn_id"] for e in of_type(world, sid, "stall_suspected")]
    assert len(of_type(world, sid, "turn_failed")) == 1


async def test_timeout_during_recovery_pastes_nothing(world: World) -> None:
    m, cfg, sid, claude_sid = await start(world, [CRASH, RESUME], limits={"turn_max_seconds": 1.5})
    slow_relaunch(cfg, [CRASH, RESUME], 3.0)
    record = await m.router(sid).submit_turn("go now")

    await world.until(lambda: record.state != "running")
    assert record.state == "timeout"
    assert m.get(sid)["state"] == "creating"
    await world.until(lambda: m.get(sid)["state"] == "ready")
    await world.until(lambda: m.router(sid).session.restarts == 1)
    assert of_type(world, sid, "turn_resumed") == []
    assert len(of_type(world, sid, "turn_failed")) == 1
    assert not [p for p in user_prompts(transcript(world, sid, claude_sid)) if "Resume" in p]


async def test_interrupt_during_recovery_ends_turn(world: World) -> None:
    m, cfg, sid, claude_sid = await start(world, [CRASH, RESUME])
    slow_relaunch(cfg, [CRASH, RESUME], 2.0)
    router = m.router(sid)
    record = await router.submit_turn("go now")
    await world.until(lambda: m.get(sid)["state"] == "creating")

    assert await router.interrupt() == record.turn_id
    assert record.state == "interrupted"
    assert of_type(world, sid, "interrupted") == [{"turn_id": record.turn_id}]
    await world.until(lambda: m.get(sid)["state"] == "ready")
    assert router.session.current_turn is None
    assert of_type(world, sid, "turn_resumed") == []
    assert [e["record"]["state"] for e in of_type(world, sid, "turn_failed")] == ["interrupted"]
    assert not [p for p in user_prompts(transcript(world, sid, claude_sid)) if "Resume" in p]
    nxt = await router.submit_turn("go again")  # the slot is free
    assert nxt.state == "running"


async def test_destroy_during_recovery_fails_turn(world: World) -> None:
    m, cfg, sid, claude_sid = await start(world, [CRASH, RESUME])
    slow_relaunch(cfg, [CRASH, RESUME], 2.0)
    record = await m.router(sid).submit_turn("go now")
    await world.until(lambda: m.get(sid)["state"] == "creating")

    await m.destroy(sid)
    assert record.state == "failed" and record.error == "session_stopped"
    failed = of_type(world, sid, "turn_failed")
    assert [e["record"]["error"] for e in failed] == ["session_stopped"]
    assert of_type(world, sid, "turn_resumed") == []
    assert not [p for p in user_prompts(transcript(world, sid, claude_sid)) if "Resume" in p]
