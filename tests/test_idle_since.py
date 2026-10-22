"""`idle_since` in the snapshot and `since` on every `session_state` event."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from claude_code_wrapper.claude_env import ClaudeEnv
from claude_code_wrapper.config import BootConfig
from claude_code_wrapper.manager import SessionManager
from claude_code_wrapper.router import SessionRouter
from claude_code_wrapper.session import Session, SessionListener, SessionSpec, build_env

SID = "ses_0123456789ab"
BOOT = {"bypass_dialog": True, "min_seconds": 0.1}
CRASH = {
    "boot": BOOT,
    "turns": [{"match": "crash", "steps": [{"text": "about to crash"}, {"exit": 3}]}],
}


class Recorder(SessionListener):
    def __init__(self) -> None:
        self.session: Session | None = None
        self.states: list[tuple[str, str | None]] = []
        self.restarts: list[tuple[int, bool]] = []

    def bind(self, session: Session) -> None:
        self.session = session

    def on_state(self, state: str) -> None:
        assert self.session is not None
        self.states.append((state, self.session.state_since))

    def on_restart(self, count: int, resumed: bool) -> None:
        self.restarts.append((count, resumed))


@pytest.fixture
def make_session(tmp_path: Path, fake_claude_bin: Path, scenario_file):
    def make(scenario: dict[str, Any] | None = None) -> tuple[Session, Recorder]:
        session_dir = tmp_path / "sessions" / SID
        config_dir = session_dir / "config"
        workdir = tmp_path / "workdir"
        config_dir.mkdir(parents=True, exist_ok=True)
        workdir.mkdir(parents=True, exist_ok=True)
        env = build_env(
            service_env=dict(os.environ),
            claude_env=ClaudeEnv(
                extra={"FAKE_CLAUDE_SCENARIO": str(scenario_file(scenario or {"boot": BOOT}))}
            ),
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
            binary=str(fake_claude_bin),
            model="",
            effort="",
            extra_args=[],
            env=env,
            boot=BootConfig(timeout_seconds=5.0, min_boot_seconds=0.2, dialogs=[]),
            max_restarts=3,
        )
        rec = Recorder()
        return Session(spec, rec), rec

    return make


async def _until(pred, timeout: float = 15.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def _compose(live: dict[str, Any], state: str) -> dict[str, Any]:
    return SessionManager._compose(
        {"session_id": SID},
        state=state,
        env={},
        turns_completed=0,
        current=None,
        live=live,
        last_activity_at=None,
    )


async def test_ready_session_has_idle_since(make_session) -> None:
    session, rec = make_session()
    try:
        assert session.snapshot()["idle_since"] is None
        await session.start()
        snap = session.snapshot()
        ready_since = dict(rec.states)["ready"]
        assert snap["idle_since"] == ready_since
        assert snap["idle_since"].endswith("Z")
        assert _compose(snap, "ready")["idle_since"] == ready_since
    finally:
        await session.stop(grace=1.0)


async def test_idle_since_cleared_when_busy_and_reset_on_completion(make_session) -> None:
    session, rec = make_session()
    try:
        await session.start()
        first = session.idle_since
        assert first
        await asyncio.sleep(0.01)
        session._set_state("busy")
        assert session.snapshot()["idle_since"] is None
        assert _compose(session.snapshot(), "busy")["idle_since"] is None
        await asyncio.sleep(0.01)
        session._set_state("ready")
        later = session.snapshot()["idle_since"]
        assert later and later > first
        assert later == session.state_since == rec.states[-1][1]
        # a non-ready state (stopped sessions loaded from disk, closing) reports null
        assert _compose({"idle_since": later}, "stopped")["idle_since"] is None
        assert _compose({}, "stopped")["idle_since"] is None
    finally:
        await session.stop(grace=1.0)


async def test_session_state_event_carries_since(make_session) -> None:
    session, rec = make_session()
    emitted: list[tuple[str, dict[str, Any]]] = []
    try:
        await session.start()
        router = SessionRouter.__new__(SessionRouter)
        router._session = session
        router._emit = lambda t, d: emitted.append((t, d))  # type: ignore[method-assign]
        router._abort_runner = lambda *_: None  # type: ignore[method-assign]
        router.ensure_tailing = lambda: None  # type: ignore[method-assign]
        router._start_ticks = lambda: None  # type: ignore[method-assign]
        router.peer_inbox = None
        router.commands = type("C", (), {"pending": staticmethod(list)})()
        router._subagents = set()
        router._metrics = None
        router._spawn = lambda coro: coro.close()  # type: ignore[method-assign]
        router._fire = lambda *a, **k: asyncio.sleep(0)  # type: ignore[method-assign]
        router.on_state("ready")
        router.on_restart(1, True)
    finally:
        await session.stop(grace=1.0)
    (t1, d1), (t2, d2) = emitted
    assert t1 == t2 == "session_state"
    assert d1["state"] == "ready" and d1["since"] == dict(rec.states)["ready"]
    assert d2["restart"] == {"count": 1, "resumed": True}
    assert d2["since"] == dict(rec.states)["ready"]
    assert all(since for _, since in rec.states)  # the creating -> ready events too


async def test_idle_since_after_restart_and_ready_at_relation(make_session) -> None:
    session, rec = make_session(CRASH)
    try:
        await session.start()
        boot_ready_at = session.ready_at
        first_idle = session.idle_since
        assert first_idle and boot_ready_at
        await session.pty.send_prompt("crash now", 0.2)
        await _until(lambda: rec.restarts and session.state == "ready")
        snap = session.snapshot()
        assert [s for s, _ in rec.states] == ["creating", "ready", "creating", "ready"]
        assert snap["idle_since"] == rec.states[-1][1]
        assert snap["idle_since"] > first_idle
        assert snap["ready_at"] > boot_ready_at  # the relaunch's boot
        assert snap["ready_at"] <= snap["idle_since"]
    finally:
        await session.stop(grace=1.0)
