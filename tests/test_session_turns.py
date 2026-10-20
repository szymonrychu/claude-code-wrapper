"""Tests for turn control on `Session` (src/claude_code_wrapper/session.py).

Every test drives the scripted fake claude under a real PTY. The fake's settings.json Stop hook
appends each payload to `hooks.jsonl`; the test feeds those payloads to `session.complete`, the
way the event router does with the relayed Stop hook. Where the router would react to a
transcript line (the interrupt line, a probe marker), the test watches the transcript and makes
the same call.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from claude_code_wrapper.claude_env import ClaudeEnv
from claude_code_wrapper.config import BootConfig, BootDialog, LimitsConfig, SessionsConfig
from claude_code_wrapper.pty_proc import paste_bytes
from claude_code_wrapper.session import (
    DEFAULT_PROBE_TEXT,
    BootError,
    Session,
    SessionListener,
    SessionSpec,
    TurnRejected,
    build_env,
)
from claude_code_wrapper.turns import TurnRecord, TurnStore

SID = "ses_0123456789ab"
BYPASS = BootDialog(pattern="Bypass Permissions mode", keys=["\x1b[B", "\r"], delay_seconds=0.05)
BOOT = {
    "timeout_seconds": 5.0,
    "min_boot_seconds": 0.2,
    "idle_ready_seconds": 30.0,
    "submit_delay_seconds": 0.15,
    "dialogs": [BYPASS],
}
FAKE_BOOT = {"bypass_dialog": True, "min_seconds": 0.1}
ESC = b"\x1b"
INTERRUPT_LINE = "[Request interrupted by user"


async def wait_until(pred: Callable[[], Any], timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


class Recorder(SessionListener):
    def __init__(self) -> None:
        self.states: list[str] = []
        self.stalls: list[str] = []
        self.ended: list[TurnRecord] = []
        self.fatals: list[str] = []
        self.restarts: list[tuple[int, bool]] = []

    def on_state(self, state: str) -> None:
        self.states.append(state)

    def on_stall(self, turn_id: str) -> None:
        self.stalls.append(turn_id)

    def on_turn_end(self, record: Any) -> None:
        self.ended.append(record.model_copy(deep=True))

    def on_fatal(self, text: str, tail: str) -> None:
        self.fatals.append(text)

    def on_restart(self, count: int, resumed: bool) -> None:
        self.restarts.append((count, resumed))


class Harness:
    """One fake-claude session with a turn store, a Stop-hook capture and a write spy."""

    def __init__(self, tmp_path: Path, fake_claude_bin: Path, scenario_file) -> None:
        self.tmp_path = tmp_path
        self.bin = fake_claude_bin
        self.scenario_file = scenario_file
        self.hooks = tmp_path / "hooks.jsonl"
        self.turns_path = tmp_path / "sessions" / SID / "turns.jsonl"
        self.writes: list[bytes] = []
        self.listener = Recorder()
        self.session: Session | None = None
        self.store: TurnStore | None = None
        self.made: list[Session] = []

    def build(
        self,
        scenario: dict | None = None,
        *,
        sessions: dict | None = None,
        limits: dict | None = None,
        binary: str | None = None,
        max_restarts: int = 3,
    ) -> Session:
        scenario = {"boot": FAKE_BOOT, **(scenario or {})}
        session_dir = self.tmp_path / "sessions" / SID
        config_dir = session_dir / "config"
        workdir = self.tmp_path / "workdir" / "proj"
        config_dir.mkdir(parents=True, exist_ok=True)
        workdir.mkdir(parents=True, exist_ok=True)
        hook = f"cat >> '{self.hooks}'; echo >> '{self.hooks}'"
        (config_dir / "settings.json").write_text(
            json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": hook}]}]}})
        )
        env = build_env(
            service_env=dict(os.environ),
            claude_env=ClaudeEnv(extra={"FAKE_CLAUDE_SCENARIO": str(self.scenario_file(scenario))}),
            config_dir=str(config_dir),
            gitconfig=str(session_dir / "gitconfig"),
            session_id=SID,
            internal_url="http://127.0.0.1:8090",
            hook_token="hook-token-0123456789",
            gate_fail="allow",
            gate_timeout=14.0,
            mise_env={},
            passthrough=[],
        )
        spec = SessionSpec(
            session_id=SID,
            claude_session_id=str(uuid.uuid4()),
            workdir=str(workdir),
            config_dir=str(config_dir),
            session_dir=str(session_dir),
            binary=binary or str(self.bin),
            model="",
            effort="",
            extra_args=[],
            env=env,
            boot=BootConfig(**BOOT),
            max_restarts=max_restarts,
            sessions=SessionsConfig(**{"interrupt_settle_seconds": 5.0, **(sessions or {})}),
            limits=LimitsConfig(**(limits or {})),
        )
        self.store = TurnStore(self.turns_path)
        self.session = Session(spec, self.listener, turn_store=self.store)
        self.made.append(self.session)
        return self.session

    async def start(self, scenario: dict | None = None, **kw: Any) -> Session:
        session = self.build(scenario, **kw)
        await session.start()
        self.spy()
        return session

    def spy(self) -> None:
        """Record every byte written to the current PTY, write by write."""
        assert self.session is not None and self.session.pty is not None
        pty = self.session.pty
        original = pty._write_locked

        async def recording(data: bytes) -> None:
            self.writes.append(bytes(data))
            await original(data)

        setattr(pty, "_write_locked", recording)  # noqa: B010 - instance-level spy

    def stops(self) -> list[dict]:
        if not self.hooks.exists():
            return []
        return [json.loads(line) for line in self.hooks.read_text().splitlines() if line.strip()]

    def transcript(self) -> str:
        assert self.session is not None
        path = Path(self.session.transcript_path)
        return path.read_text() if path.exists() else ""

    async def next_stop(self, count: int = 1, timeout: float = 10.0) -> dict:
        await wait_until(lambda: len(self.stops()) >= count, timeout)
        return self.stops()[count - 1]


@pytest.fixture
async def harness(tmp_path: Path, fake_claude_bin: Path, scenario_file):
    h = Harness(tmp_path, fake_claude_bin, scenario_file)
    yield h
    for session in h.made:
        await session.stop(grace=1.0)


def marker(turn_id: str) -> str:
    return f"\n\n<!-- ccw turn: {turn_id} -->"


# ------------------------------------------------------------------ submit


async def test_submit_writes_paste_then_cr_and_marker(harness: Harness) -> None:
    session = await harness.start({"turns": [{"match": "hello", "steps": [{"text": "hi back"}]}]})
    seen_at_first_write: list[tuple[str, TurnRecord | None]] = []
    original = session.pty._write_locked

    async def check_reserved(data: bytes) -> None:
        if not seen_at_first_write:
            seen_at_first_write.append((session.state, harness.store.current))
        await original(data)

    setattr(session.pty, "_write_locked", check_reserved)  # noqa: B010 - instance-level spy
    record = await session.submit("hello there", callback_url="https://cb.example/turn")
    # The slot was reserved and the record persisted before the first byte went out.
    state_then, current_then = seen_at_first_write[0]
    assert state_then == "busy"
    assert current_then is not None and current_then.turn_id == record.turn_id
    assert record.turn_id.startswith("turn-")
    assert record.state == "running" and record.origin == "client"
    assert record.session_id == SID and record.text == "hello there"
    assert record.callback_url == "https://cb.example/turn"
    assert record.started_at and record.last_activity_at
    assert session.state == "busy" and session.snapshot()["state"] == "busy"
    assert session.current_turn is record
    # Exactly two writes: the bracketed paste with the marker, then a lone CR.
    prompt = "hello there" + marker(record.turn_id)
    assert harness.writes == [paste_bytes(prompt), b"\r"]
    await wait_until(lambda: f"<!-- ccw turn: {record.turn_id} -->" in harness.transcript())
    stop = await harness.next_stop()
    assert stop["last_assistant_message"] == "hi back"
    assert session.complete(stop, marker_turn_id=record.turn_id) is record
    assert session.state == "ready"

    # turn_marker off: the text is pasted as is.
    await session.stop(grace=1.0)
    harness.writes.clear()
    session = await harness.start(sessions={"turn_marker": False})
    plain = await session.submit("no marker please")
    assert harness.writes == [paste_bytes("no marker please"), b"\r"]
    assert plain.text == "no marker please"


async def test_submit_busy_rejected(harness: Harness) -> None:
    session = await harness.start({"turns": [{"match": "slow", "steps": [{"sleep": 30}]}]})
    record = await session.submit("slow one")
    writes = list(harness.writes)
    with pytest.raises(TurnRejected) as exc:
        await session.submit("second")
    assert exc.value.reason == "busy"
    assert str(exc.value) == "busy"
    assert harness.writes == writes  # nothing written for the rejected turn
    assert session.current_turn is record
    assert [r.turn_id for r in harness.store.list()] == [record.turn_id]


async def test_submit_booting_and_dead_rejected(harness: Harness, tmp_path: Path) -> None:
    session = harness.build()
    assert session.state == "booting"
    with pytest.raises(TurnRejected) as exc:
        await session.submit("too early")
    assert exc.value.reason == "booting"
    # A session whose claude could not be launched is dead.
    dead = harness.build(binary=str(tmp_path / "no-such-claude"))
    with pytest.raises(BootError):
        await dead.start()
    assert dead.state == "dead"
    with pytest.raises(TurnRejected) as exc:
        await dead.submit("anyone there")
    assert exc.value.reason == "dead"
    # A stopped session rejects as dead too.
    harness.session = session = harness.build()
    await session.start()
    await session.stop(grace=1.0)
    with pytest.raises(TurnRejected) as exc:
        await session.submit("after stop")
    assert exc.value.reason == "dead"
    assert harness.store.list() == []


# ------------------------------------------------------------------ Stop


async def test_complete_from_stop_payload(harness: Harness) -> None:
    session = await harness.start({"turns": [{"match": "answer", "steps": [{"text": "42"}]}]})
    record = await session.submit("the answer please")
    stop = await harness.next_stop()
    usage = {"input_tokens": 3, "output_tokens": 1}
    tokens = {"fake-claude": {"input_tokens": 3, "output_tokens": 1}}
    done = session.complete(stop, usage=usage, model_tokens=tokens, marker_turn_id=record.turn_id)
    assert done is record
    assert done.state == "completed"
    assert done.final_text == "42"
    assert done.stop_reason == "end_turn"
    assert done.usage == usage and done.model_tokens == tokens
    assert done.completed_at and done.error is None
    assert session.state == "ready" and session.current_turn is None
    stored = harness.store.get(record.turn_id)
    assert stored is not None and stored.state == "completed" and stored.final_text == "42"
    assert [r.turn_id for r in harness.listener.ended] == [record.turn_id]
    assert harness.listener.ended[0].state == "completed"
    # A late copy of that Stop, carrying the finished turn's marker, cannot end the next turn.
    nxt = await session.submit("the answer again")
    assert session.complete(stop, marker_turn_id=record.turn_id) is None
    assert session.current_turn is nxt and nxt.state == "running"
    # No marker known yet (the transcript lagged): the Stop still completes the running turn.
    stop2 = await harness.next_stop(2)
    assert session.complete(stop2) is nxt and nxt.state == "completed"
    assert len(harness.listener.ended) == 2


async def test_stale_stop_ignored(harness: Harness, tmp_path: Path) -> None:
    session = await harness.start({"turns": [{"match": "slow", "steps": [{"sleep": 30}]}]})
    record = await session.submit("slow one")
    claude_sid = session.spec.claude_session_id
    other = {"session_id": str(uuid.uuid4()), "last_assistant_message": "not yours"}
    assert session.complete(other) is None
    assert session.complete(other, marker_turn_id=record.turn_id) is None
    stale = {"session_id": claude_sid, "last_assistant_message": "an older turn"}
    assert session.complete(stale, marker_turn_id="turn-0older") is None
    assert session.complete({"last_assistant_message": "no session id"}) is None
    assert session.state == "busy" and session.current_turn is record
    assert record.state == "running" and record.final_text is None
    assert harness.store.get(record.turn_id).state == "running"
    assert harness.listener.ended == []
    # Booting and dead sessions accept no Stop at all, not even as agent turns.
    booting = harness.build()
    assert booting.complete({"session_id": booting.spec.claude_session_id}) is None
    dead = harness.build(binary=str(tmp_path / "no-such-claude"))
    with pytest.raises(BootError):
        await dead.start()
    assert dead.complete({"session_id": dead.spec.claude_session_id}) is None
    persisted = TurnStore(harness.turns_path).list()
    assert [(r.turn_id, r.state) for r in persisted] == [(record.turn_id, "running")]


async def test_unsolicited_stop_creates_agent_turn(harness: Harness) -> None:
    scenario = {
        "turns": [
            {
                "match": "workflow",
                "steps": [
                    {"text": "started a background workflow"},
                    {"background": {"after": 0.5, "text": "background workflow finished"}},
                ],
            }
        ]
    }
    session = await harness.start(scenario)
    record = await session.submit("run the workflow")
    first = await harness.next_stop()
    assert session.complete(first, marker_turn_id=record.turn_id) is record
    assert session.state == "ready"
    background = await harness.next_stop(2)
    assert background["last_assistant_message"] == "background workflow finished"
    # The router still knows the finished turn's marker; an idle session takes the Stop anyway.
    agent = session.complete(background, usage={"output_tokens": 3}, marker_turn_id=record.turn_id)
    assert agent is not None and agent.turn_id != record.turn_id
    assert agent.origin == "agent" and agent.state == "completed"
    assert agent.final_text == "background workflow finished"
    assert agent.stop_reason == "end_turn" and agent.usage == {"output_tokens": 3}
    assert agent.session_id == SID and agent.started_at and agent.completed_at
    assert session.state == "ready" and session.current_turn is None
    assert [r.turn_id for r in harness.store.list()] == [record.turn_id, agent.turn_id]
    assert harness.store.get(agent.turn_id).origin == "agent"
    assert [r.origin for r in harness.listener.ended] == ["client", "agent"]
    assert "busy" not in harness.listener.states[harness.listener.states.index("ready", 2) :]


# ------------------------------------------------------------------ interrupt


async def test_interrupt_fails_turn_interrupted(harness: Harness) -> None:
    session = await harness.start({"turns": [{"match": "slow", "steps": [{"sleep": 30}]}]})
    assert await session.interrupt() is None  # idle: nothing to interrupt, nothing written
    assert harness.writes == []
    record = await session.submit("slow one")
    await wait_until(lambda: "slow one" in harness.transcript())
    started = time.monotonic()
    task = asyncio.ensure_future(session.interrupt())
    # The router: the transcript interrupt line arrives -> on_interrupted().
    await wait_until(lambda: INTERRUPT_LINE in harness.transcript())
    session.on_interrupted()
    assert await asyncio.wait_for(task, 5) == record.turn_id
    assert time.monotonic() - started < session.spec.sessions.interrupt_settle_seconds
    assert record.state == "interrupted" and record.completed_at
    assert harness.writes.count(ESC) == 1  # a single ESC, no escalation
    assert session.state == "ready" and session.current_turn is None
    assert harness.store.get(record.turn_id).state == "interrupted"
    assert [(r.turn_id, r.state) for r in harness.listener.ended] == [
        (record.turn_id, "interrupted")
    ]
    # The slot is free: the next turn goes through.
    nxt = await session.submit("another slow one")
    assert session.current_turn is nxt
    # Unprompted: the transcript shows an interrupt (a human pressed ESC): the turn ends too.
    await wait_until(lambda: harness.transcript().count("another slow one") >= 1)
    session.on_interrupted()
    assert nxt.state == "interrupted" and session.state == "ready"
    session.on_interrupted()  # idle: a late duplicate line changes nothing
    assert len(harness.listener.ended) == 2


async def test_interrupt_escalates_after_settle(harness: Harness) -> None:
    # The fake prints and goes idle without a Stop hook; an ESC at its prompt does nothing, so
    # no interrupt line ever reaches the transcript: the classic wedged slot.
    scenario = {"turns": [{"match": "wedge", "steps": [{"print": "working...\n"}]}]}
    session = await harness.start(scenario, sessions={"interrupt_settle_seconds": 0.4})
    record = await session.submit("wedge yourself")
    await wait_until(lambda: session.ring.contains("working..."))
    started = time.monotonic()
    assert await session.interrupt() == record.turn_id
    assert time.monotonic() - started >= 0.4
    assert harness.writes[-2:] == [ESC, ESC]
    assert harness.writes.count(ESC) == 2
    assert record.state == "interrupted"
    assert session.state == "ready" and session.current_turn is None
    assert harness.store.get(record.turn_id).state == "interrupted"
    assert len(harness.listener.ended) == 1
    # A second interrupt while idle is a no-op; the next turn is accepted.
    assert await session.interrupt() is None
    nxt = await session.submit("wedge again")
    assert session.current_turn is nxt


async def test_concurrent_interrupts_share_one_escalation(harness: Harness) -> None:
    scenario = {"turns": [{"match": "wedge", "steps": [{"print": "working...\n"}]}]}
    session = await harness.start(scenario, sessions={"interrupt_settle_seconds": 0.3})
    record = await session.submit("wedge yourself")
    await wait_until(lambda: session.ring.contains("working..."))
    results = await asyncio.gather(session.interrupt(), session.interrupt())
    assert results == [record.turn_id, record.turn_id]
    assert harness.writes.count(ESC) == 2
    assert len(harness.listener.ended) == 1


async def test_stop_hook_during_interrupt_completes_turn(harness: Harness) -> None:
    scenario = {"turns": [{"match": "wedge", "steps": [{"print": "working...\n"}]}]}
    session = await harness.start(scenario, sessions={"interrupt_settle_seconds": 5.0})
    record = await session.submit("wedge yourself")
    task = asyncio.ensure_future(session.interrupt())
    await wait_until(lambda: ESC in harness.writes)
    stop = {"session_id": session.spec.claude_session_id, "last_assistant_message": "done"}
    assert session.complete(stop, marker_turn_id=record.turn_id) is record
    assert await asyncio.wait_for(task, 2) == record.turn_id
    assert record.state == "completed"  # the Stop won the race, the interrupt does not undo it
    assert harness.writes.count(ESC) == 1
    assert session.state == "ready"


# ------------------------------------------------------------------ fatal, stall, limits


async def test_fatal_fails_turn(harness: Harness) -> None:
    scenario = {
        "turns": [{"match": "auth", "steps": [{"print": "\x1b[31mAPI Error: 401\x1b[0m bad key"}]}]
    }
    session = await harness.start(scenario)
    record = await session.submit("auth check")
    await wait_until(lambda: record.state != "running")
    assert record.state == "failed"
    assert record.error == "API Error: 401"
    assert record.completed_at
    assert harness.listener.fatals == ["API Error: 401"]
    assert session.state == "ready" and session.current_turn is None
    assert harness.store.get(record.turn_id).error == "API Error: 401"
    assert [r.state for r in harness.listener.ended] == ["failed"]
    nxt = await session.submit("an ordinary prompt")
    assert session.current_turn is nxt
    stop = await harness.next_stop()
    assert session.complete(stop) is nxt
    # A prompt that quotes a fatal pattern is echoed on screen; that does not fail its own turn.
    quoted = await session.submit("what does API Error: 401 mean?")
    await wait_until(lambda: session.ring.contains("echo: what does API Error: 401 mean?"))
    stop = await harness.next_stop(2)
    assert harness.listener.fatals[-1] == "API Error: 401"
    assert quoted.state == "running"
    assert session.complete(stop) is quoted and quoted.state == "completed"


async def test_stall_reported_not_failed(harness: Harness) -> None:
    scenario = {"turns": [{"match": "quiet", "steps": [{"sleep": 1.6}, {"text": "back"}]}]}
    session = await harness.start(scenario, sessions={"turn_stall_seconds": 0.4})
    record = await session.submit("quiet for a while")
    await wait_until(lambda: harness.listener.stalls, timeout=3)
    assert harness.listener.stalls == [record.turn_id]
    assert record.stall_suspected_since is not None
    assert harness.store.get(record.turn_id).stall_suspected_since == record.stall_suspected_since
    assert record.state == "running" and session.state == "busy"
    await asyncio.sleep(0.6)
    assert harness.listener.stalls == [record.turn_id]  # once per quiet period, not repeatedly
    # Activity resets the timer and clears the suspicion; a new quiet period reports again.
    before = record.last_activity_at
    await asyncio.sleep(0.01)
    session.note_activity()
    assert record.stall_suspected_since is None
    assert record.last_activity_at and record.last_activity_at > before
    await wait_until(lambda: len(harness.listener.stalls) == 2, timeout=3)
    stop = await harness.next_stop()
    assert session.complete(stop, marker_turn_id=record.turn_id) is record
    assert record.state == "completed" and record.final_text == "back"
    assert [r.state for r in harness.listener.ended] == ["completed"]
    session.note_activity()  # idle: harmless


async def test_activity_keeps_stall_away(harness: Harness) -> None:
    scenario = {"turns": [{"match": "busy", "steps": [{"sleep": 30}]}]}
    session = await harness.start(scenario, sessions={"turn_stall_seconds": 0.4})
    await session.submit("busy working")
    for _ in range(8):
        await asyncio.sleep(0.15)
        session.note_activity()
    assert harness.listener.stalls == []


async def test_turn_max_seconds_times_out(harness: Harness) -> None:
    scenario = {"turns": [{"match": "forever", "steps": [{"sleep": 30}]}]}
    session = await harness.start(
        scenario,
        sessions={"interrupt_settle_seconds": 5.0},
        limits={"turn_max_seconds": 0.5},
    )
    record = await session.submit("run forever")
    # The router reacting to the interrupt line the hard limit's ESC produces.
    await wait_until(lambda: ESC in harness.writes, timeout=3)
    await wait_until(lambda: INTERRUPT_LINE in harness.transcript())
    session.on_interrupted()
    await wait_until(lambda: record.state != "running", timeout=3)
    assert record.state == "timeout"
    assert harness.writes.count(ESC) == 1
    assert session.state == "ready" and session.current_turn is None
    assert harness.store.get(record.turn_id).state == "timeout"
    assert [r.state for r in harness.listener.ended] == ["timeout"]

    # Without the interrupt line, the hard limit escalates and still ends the turn as timeout.
    await session.stop(grace=1.0)
    harness.writes.clear()
    harness.listener.ended.clear()
    wedge = {"turns": [{"match": "wedge", "steps": [{"print": "working...\n"}]}]}
    session = await harness.start(
        wedge,
        sessions={"interrupt_settle_seconds": 0.3},
        limits={"turn_max_seconds": 0.4},
    )
    record = await session.submit("wedge yourself")
    await wait_until(lambda: record.state != "running", timeout=5)
    assert record.state == "timeout"
    assert harness.writes.count(ESC) == 2
    assert session.state == "ready"
    assert [r.state for r in harness.listener.ended] == ["timeout"]


# ------------------------------------------------------------------ probe


async def test_probe_delivered(harness: Harness) -> None:
    session = await harness.start({"turns": [{"match": "slow", "steps": [{"sleep": 30}]}]})
    # Ready: the probe does not take the slot.
    probe = await session.probe()
    assert probe.probe_id.startswith("probe-") and probe.state == "pending"
    assert probe.text == DEFAULT_PROBE_TEXT
    assert DEFAULT_PROBE_TEXT == (
        "Reply with CCW-ALIVE and one sentence on what you are doing, then continue."
    )
    tag = f"<!-- ccw probe: {probe.probe_id} -->"
    assert harness.writes == [paste_bytes(f"{DEFAULT_PROBE_TEXT}\n\n{tag}"), b"\r"]
    assert session.state == "ready" and session.current_turn is None
    assert harness.store.list() == []
    assert session.get_probe(probe.probe_id) is probe
    assert session.get_probe("probe-unknown") is None
    # The router sees the marker in the transcript.
    await wait_until(lambda: tag in harness.transcript())
    assert session.probe_delivered(probe.probe_id) is probe
    assert probe.state == "delivered" and probe.delivered_at
    assert session.probe_delivered(probe.probe_id) is probe  # idempotent
    assert session.probe_delivered("probe-unknown") is None
    # ...then assistant text containing CCW-ALIVE.
    assert session.probe_answered() == [probe]
    assert probe.state == "answered" and probe.answered_at
    assert session.probe_answered() == []

    # Busy: written alongside the running turn, which keeps the slot.
    await harness.next_stop()  # the probe's own reply ran a turn in the fake; let it settle
    record = await session.submit("slow one")
    harness.writes.clear()
    busy_probe = await session.probe("custom check")
    assert busy_probe.text == "custom check"
    assert harness.writes == [
        paste_bytes(f"custom check\n\n<!-- ccw probe: {busy_probe.probe_id} -->"),
        b"\r",
    ]
    assert session.state == "busy" and session.current_turn is record
    assert record.state == "running"
    assert session.probe_answered() == []  # not delivered yet, so nothing to answer


async def test_probe_rejected_when_not_running(harness: Harness) -> None:
    session = harness.build()
    with pytest.raises(TurnRejected) as exc:
        await session.probe()
    assert exc.value.reason == "booting"
    await session.start()
    await session.stop(grace=1.0)
    with pytest.raises(TurnRejected) as exc:
        await session.probe()
    assert exc.value.reason == "dead"


# ------------------------------------------------------------------ exit, stop, tool calls


async def test_exit_during_turn_fails_turn(harness: Harness) -> None:
    scenario = {
        "turns": [{"match": "crash", "steps": [{"text": "about to crash"}, {"exit": 3}]}],
    }
    session = await harness.start(scenario)
    record = await session.submit("crash now")
    await wait_until(lambda: record.state != "running")
    assert record.state == "failed" and record.error == "claude_exited"
    assert harness.store.get(record.turn_id).error == "claude_exited"
    assert [r.error for r in harness.listener.ended] == ["claude_exited"]
    assert session.current_turn is None
    # claude is relaunched (resumed), and the prompt is not pasted again.
    await wait_until(lambda: harness.listener.restarts, timeout=15)
    assert session.state == "ready"
    await asyncio.sleep(0.5)
    prompts = [
        line
        for line in harness.transcript().splitlines()
        if '"type": "user"' in line and "crash now" in line
    ]
    assert len(prompts) == 1
    assert harness.listener.states[-3:] == ["busy", "creating", "ready"]
    nxt = await session.submit("still here")
    assert session.current_turn is nxt


async def test_exit_while_ready_rejects_submit_until_relaunched(harness: Harness) -> None:
    session = await harness.start()
    await session.pty.write(b"\x03")  # the fake exits 0 at idle
    await wait_until(lambda: session.state != "ready", timeout=5)
    with pytest.raises(TurnRejected) as exc:
        await session.submit("during relaunch")
    assert exc.value.reason == "booting"
    await wait_until(lambda: harness.listener.restarts, timeout=15)
    assert session.state == "ready"


async def test_stop_during_turn_fails_turn(harness: Harness) -> None:
    session = await harness.start({"turns": [{"match": "slow", "steps": [{"sleep": 30}]}]})
    record = await session.submit("slow one")
    await session.stop(grace=1.0)
    assert record.state == "failed" and record.error == "session_stopped"
    assert harness.store.get(record.turn_id).state == "failed"
    assert [r.state for r in harness.listener.ended] == ["failed"]
    assert session.state == "stopped"


async def test_record_tool_call_counts(harness: Harness) -> None:
    session = await harness.start({"turns": [{"match": "slow", "steps": [{"sleep": 30}]}]})
    session.record_tool_call()  # idle: no-op, no record appears
    assert harness.store.list() == []
    record = await session.submit("slow one")
    session.record_tool_call()
    session.record_tool_call()
    assert record.tool_calls == 2
    assert harness.store.get(record.turn_id).tool_calls == 2
    assert TurnStore(harness.turns_path).get(record.turn_id).tool_calls == 2
    assert session.current_turn is record and record.state == "running"


async def test_note_activity_keeps_store_view_current(harness: Harness) -> None:
    scenario = {"turns": [{"match": "busy", "steps": [{"sleep": 30}]}]}
    session = await harness.start(scenario)
    record = await session.submit("busy working")
    lines_before = Path(harness.store.path).read_text().count("\n")
    for _ in range(3):
        await asyncio.sleep(0.01)
        session.note_activity()
        # Every activity is visible through the store at once ...
        assert harness.store.get(record.turn_id).last_activity_at == record.last_activity_at
        assert harness.store.current.last_activity_at == record.last_activity_at
    # ... while the hot path appends at most one line per ACTIVITY_PERSIST_SECONDS.
    assert Path(harness.store.path).read_text().count("\n") == lines_before
    await session.interrupt()
