"""A turn cut off by a service shutdown continues on resume (design D5), against the fake claude.

A graceful shutdown leaves the running turn `running` in turns.jsonl with the marker
`interrupted: "shutdown"` and emits no `turn_failed`; a user stop or delete still fails it
`session_stopped`. On resume (the API resume or `load_persisted`) the fresh session adopts the
running record: with `sessions.restart_continue_prompt` set it pastes the prompt with the same
turn marker (`turn_resumed`, cause `resume`) and the same turn completes; without it the turn
fails `service_shutdown`. A turn a killed process left `running` is treated the same way.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from conftest import World, jsonl, turns_of, wait_until
from test_session_turns import ESC, Harness, harness  # noqa: F401 - the fixture is used by name
from test_turn_continuity import of_type, transcript, user_prompts

from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.config import Config
from claude_code_wrapper.manager import CreateSessionRequest, SessionManager
from claude_code_wrapper.turns import TurnStore

TOKEN = "tok-shutdown-0123456789"
CONTINUE = "Resume: the service restarted mid-turn, continue the task"
SLOW = {"match": "^slow", "steps": [{"text": "part one"}, {"sleep": 30}]}
RESUME = {"match": "^Resume", "steps": [{"text": "part two"}]}


def config(
    world: World, prompt: str = CONTINUE, limits: dict[str, Any] | None = None, **sessions: Any
) -> Config:
    return world.cfg(
        {"turns": [SLOW, RESUME]},
        server={"auth_token": TOKEN},
        sessions={"restart_continue_prompt": prompt, **sessions},
        limits=limits or {},
    )


async def busy_session(world: World, cfg: Config) -> tuple[SessionManager, str, str, Any]:
    """A session whose turn is running (its prompt is in the transcript, claude is working)."""
    m = world.manager(cfg)
    created = await m.create(CreateSessionRequest())
    sid, claude_sid = created["session_id"], created["claude_session_id"]
    record = await m.router(sid).submit_turn("slow work", callback_url=f"{world.url}/cb")
    await world.until(
        lambda: [d for d in of_type(world, sid, "assistant_text") if d["text"] == "part one"]
    )
    assert m.get(sid)["state"] == "busy" and record.state == "running"
    return m, sid, claude_sid, record


async def shut_down(world: World, cfg: Config) -> tuple[str, str, str]:
    """A busy session's service shut down gracefully mid-turn: (sid, claude_sid, turn_id)."""
    m, sid, claude_sid, record = await busy_session(world, cfg)
    await m.shutdown()
    return sid, claude_sid, record.turn_id


def stored(world: World, sid: str) -> TurnStore:
    """turns.jsonl as a new process reads it."""
    return TurnStore(world.session_dir(sid) / "turns.jsonl", readonly=True)


def elapsed_since(iso: str) -> float:
    return (datetime.now(UTC) - datetime.fromisoformat(iso)).total_seconds()


def continued(world: World, sid: str, claude_sid: str) -> list[str]:
    return [p for p in user_prompts(transcript(world, sid, claude_sid)) if p.startswith("Resume")]


async def api_resume(m: SessionManager, cfg: Config, sid: str) -> httpx.Response:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(m, cfg)),
        base_url="http://ccw",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    async with client:
        return await client.post(f"/v1/sessions/{sid}/resume")


# ---------------------------------------------------------------- shutdown and user stop


async def test_shutdown_marks_running_turn_interrupted(world: World) -> None:
    cfg = config(world)
    m, sid, _, record = await busy_session(world, cfg)
    await m.shutdown()

    assert record.state == "running" and record.error is None
    assert record.interrupted == "shutdown"
    # Persisted, and what a restarted service reads back.
    current = stored(world, sid).current
    assert current is not None and current.turn_id == record.turn_id
    assert (current.state, current.interrupted, current.error) == ("running", "shutdown", None)
    assert jsonl(world.session_dir(sid) / "turns.jsonl")[-1]["interrupted"] == "shutdown"
    assert of_type(world, sid, "turn_failed") == []
    assert of_type(world, sid, "turn_completed") == []
    assert world.callbacks == []
    assert world.meta(sid)["state"] == "stopped"


async def test_user_stop_turn_not_continued(world: World) -> None:
    cfg = config(world)
    m, sid, claude_sid, record = await busy_session(world, cfg)
    await m.destroy(sid)

    assert (record.state, record.error, record.interrupted) == ("failed", "session_stopped", None)
    failed = of_type(world, sid, "turn_failed")
    assert [(e["record"]["turn_id"], e["record"]["error"]) for e in failed] == [
        (record.turn_id, "session_stopped")
    ]
    assert stored(world, sid).current is None

    # A resume (even with the continue prompt set) has nothing to continue.
    resumed = await m.resume(sid)
    assert resumed["state"] == "ready" and resumed["current_turn_id"] is None
    assert of_type(world, sid, "turn_resumed") == []
    assert continued(world, sid, claude_sid) == []
    assert [(t.turn_id, t.state, t.error) for t in turns_of(m, sid)] == [
        (record.turn_id, "failed", "session_stopped")
    ]
    assert len(of_type(world, sid, "turn_failed")) == 1


# ---------------------------------------------------------------- resume continues the turn


async def test_api_resume_continues_interrupted_turn(world: World) -> None:
    cfg = config(world)
    sid, claude_sid, tid = await shut_down(world, cfg)

    m2 = world.manager(cfg)  # a new service process on the same sessions_root
    await m2.load_persisted()
    assert m2.get(sid)["state"] == "stopped"
    response = await api_resume(m2, cfg, sid)
    assert response.status_code == 200, response.text
    assert response.json()["current_turn_id"] == tid
    # Adopted: the marker is cleared on disk while the continuation runs.
    assert stored(world, sid).get(tid).interrupted is None

    await world.until(lambda: m2.open_turns(sid).get(tid).state != "running")
    done = m2.open_turns(sid).get(tid)
    assert (done.state, done.final_text, done.error) == ("completed", "part two", None)
    assert done.interrupted is None
    # The usage of the leg after the resume ("part two"); the first leg died with its process.
    assert done.usage is not None and done.usage["output_tokens"] == 2
    assert [t.turn_id for t in turns_of(m2, sid)] == [tid]
    assert of_type(world, sid, "turn_resumed") == [{"turn_id": tid, "cause": "resume"}]
    assert [e["record"]["turn_id"] for e in of_type(world, sid, "turn_completed")] == [tid]
    assert of_type(world, sid, "turn_failed") == []
    msgs = [d for d in of_type(world, sid, "user_message") if d["text"] == CONTINUE]
    assert [(d["turn_id"], d["origin"]) for d in msgs] == [(tid, "restart")]
    # The original prompt is not re-pasted; the continue prompt carries the same marker.
    prompts = user_prompts(transcript(world, sid, claude_sid))
    assert [p for p in prompts if p.startswith("slow")] == [
        f"slow work\n\n<!-- ccw turn: {tid} -->"
    ]
    assert continued(world, sid, claude_sid) == [f"{CONTINUE}\n\n<!-- ccw turn: {tid} -->"]
    await world.until(lambda: world.callbacks)
    assert [(c["type"], c["data"]["record"]["turn_id"]) for c in world.callbacks] == [
        ("turn_completed", tid)
    ]
    await world.until(lambda: m2.get(sid)["state"] == "ready")


async def test_load_persisted_continues_interrupted_turn(world: World) -> None:
    cfg = config(world, resume_on_start=True)
    m1, sid, claude_sid, record = await busy_session(world, cfg)
    await m1.shutdown()
    # As a killed process leaves it: session.json still busy, the turn running with no marker.
    meta = world.meta(sid)
    meta["state"] = "busy"
    world.session_dir(sid).joinpath("session.json").write_text(json.dumps(meta))
    store = TurnStore(world.session_dir(sid) / "turns.jsonl")
    killed = store.get(record.turn_id)
    assert killed is not None
    killed.interrupted = None
    store.put(killed)
    assert stored(world, sid).current.interrupted is None

    m2 = world.manager(cfg)
    await m2.load_persisted()
    tid = record.turn_id
    await world.until(lambda: m2.open_turns(sid).get(tid).state != "running")
    done = m2.open_turns(sid).get(tid)
    assert (done.state, done.final_text) == ("completed", "part two")
    assert of_type(world, sid, "turn_resumed") == [{"turn_id": tid, "cause": "resume"}]
    assert continued(world, sid, claude_sid) == [f"{CONTINUE}\n\n<!-- ccw turn: {tid} -->"]
    assert of_type(world, sid, "turn_failed") == []
    await world.until(lambda: m2.get(sid)["state"] == "ready")


async def test_resume_without_prompt_fails_interrupted_turn(world: World) -> None:
    cfg = config(world, prompt="")
    sid, claude_sid, tid = await shut_down(world, cfg)
    current = stored(world, sid).current
    assert current is not None and current.interrupted == "shutdown"

    m2 = world.manager(cfg)
    resumed = await m2.resume(sid)
    assert resumed["state"] == "ready" and resumed["current_turn_id"] is None
    done = m2.open_turns(sid).get(tid)
    assert (done.state, done.error, done.interrupted) == ("failed", "service_shutdown", None)
    failed = of_type(world, sid, "turn_failed")
    assert [(e["record"]["turn_id"], e["record"]["error"]) for e in failed] == [
        (tid, "service_shutdown")
    ]
    assert of_type(world, sid, "turn_resumed") == []
    assert continued(world, sid, claude_sid) == []
    await world.until(lambda: world.callbacks)
    assert [(c["type"], c["data"]["record"]["error"]) for c in world.callbacks] == [
        ("turn_failed", "service_shutdown")
    ]
    # The slot is free for the next turn.
    nxt = await m2.router(sid).submit_turn("a new turn")
    await world.until(lambda: nxt.state == "completed")


async def test_turn_max_seconds_spans_resume(world: World) -> None:
    sid, claude_sid, tid = await shut_down(world, config(world))
    started = stored(world, sid).get(tid).started_at
    await world.until(lambda: elapsed_since(started) > 1.5)
    # The limit counts from the original submit: a turn already over it is not continued.
    m2 = world.manager(config(world, limits={"turn_max_seconds": 1.0}))
    await m2.resume(sid)
    await world.until(lambda: m2.open_turns(sid).get(tid).state != "running")
    assert m2.open_turns(sid).get(tid).state == "timeout"
    assert [e["record"]["state"] for e in of_type(world, sid, "turn_failed")] == ["timeout"]
    assert of_type(world, sid, "turn_resumed") == []
    assert continued(world, sid, claude_sid) == []


async def test_shutdown_during_interrupt_ends_turn(harness: Harness) -> None:  # noqa: F811
    # The fake goes idle without a Stop and ignores ESC there: the interrupt waits to settle.
    scenario = {"turns": [{"match": "wedge", "steps": [{"print": "working...\n"}]}]}
    session = await harness.start(scenario, sessions={"interrupt_settle_seconds": 5.0})
    record = await session.submit("wedge yourself")
    await wait_until(lambda: session.ring.contains("working..."))
    interrupting = asyncio.ensure_future(session.interrupt())
    await wait_until(lambda: ESC in harness.writes)
    await session.stop(grace=1.0, cause="shutdown")
    # The client asked for the turn to end: it ends as the interrupt would, never continued.
    assert (record.state, record.interrupted) == ("interrupted", None)
    assert await asyncio.wait_for(interrupting, 2) == record.turn_id
    assert harness.store.get(record.turn_id).state == "interrupted"
    assert [r.state for r in harness.listener.ended] == ["interrupted"]


async def test_shutdown_while_resuming_keeps_marker(world: World) -> None:
    cfg = config(world)
    sid, _, tid = await shut_down(world, cfg)
    slow_boot = Path(cfg.claude.env.extra["FAKE_CLAUDE_SCENARIO"])
    slow_boot.write_text(
        json.dumps({"boot": {"bypass_dialog": True, "min_seconds": 3.0}, "turns": [SLOW, RESUME]})
    )
    m2 = world.manager(cfg)
    resuming = asyncio.ensure_future(m2.resume(sid))
    await world.until(lambda: m2.active_ids())
    await m2.shutdown()
    with contextlib.suppress(BaseException):
        await resuming
    current = stored(world, sid).current
    assert current is not None and current.turn_id == tid
    assert (current.state, current.interrupted) == ("running", "shutdown")
    assert of_type(world, sid, "turn_failed") == []


async def test_event_seq_continues_across_resume(world: World) -> None:
    cfg = config(world)
    sid, _, tid = await shut_down(world, cfg)
    before = world.events(sid)
    assert before[-1]["type"] == "session_closed"
    last = before[-1]["seq"]

    m2 = world.manager(cfg)
    await m2.resume(sid)
    await world.until(lambda: m2.open_turns(sid).get(tid).state == "completed")
    await m2.shutdown()
    events = world.events(sid)
    assert events[: len(before)] == before
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
    after = events[len(before) :]
    assert after[0]["seq"] == last + 1
    resumed = [e for e in after if e["type"] == "turn_resumed"]
    assert [e["data"] for e in resumed] == [{"turn_id": tid, "cause": "resume"}]
    assert resumed[0]["seq"] > last
