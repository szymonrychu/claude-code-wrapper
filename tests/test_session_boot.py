"""Tests for the session process lifecycle (src/claude_code_wrapper/session.py).

Every boot test drives the scripted fake claude under a real PTY. The child env is minimal by
design, so the fake's FAKE_CLAUDE_* variables travel through `claude.env.extra`.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from claude_code_wrapper.claude_env import ClaudeEnv
from claude_code_wrapper.config import BootConfig, BootDialog
from claude_code_wrapper.session import (
    PUBLIC_STATES,
    BootError,
    FatalScanner,
    Session,
    SessionListener,
    SessionSpec,
    build_argv,
    build_env,
    public_state,
)

BYPASS = BootDialog(pattern="Bypass Permissions mode", keys=["\x1b[B", "\r"], delay_seconds=0.05)
FAST = {"timeout_seconds": 3.0, "min_boot_seconds": 0.2, "idle_ready_seconds": 0.2}
# Ready only on the footer marker: a quiet screen before the dialog must not count as ready.
MARKER_ONLY = {**FAST, "idle_ready_seconds": 30.0, "dialogs": [BYPASS]}
CRASH_SCENARIO = {
    "boot": {"bypass_dialog": True, "min_seconds": 0.1},
    "turns": [{"match": "crash", "steps": [{"text": "about to crash"}, {"exit": 3}]}],
}


async def wait_until(pred: Callable[[], bool], timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def gone(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    return False


class Recorder(SessionListener):
    def __init__(self) -> None:
        self.bound: Session | None = None
        self.states: list[str] = []
        self.output: list[str] = []
        self.fatals: list[tuple[str, str]] = []
        self.restarts: list[tuple[int, bool]] = []
        self.exits: list[int] = []
        self.ring_at_creating: list[str] = []

    def bind(self, session: Session) -> None:
        self.bound = session

    def on_state(self, state: str) -> None:
        self.states.append(state)
        if state == "creating" and self.bound is not None:
            self.ring_at_creating.append(self.bound.ring.text())

    def on_output(self, text_chunk: str) -> None:
        self.output.append(text_chunk)

    def on_fatal(self, text: str, tail: str) -> None:
        self.fatals.append((text, tail))

    def on_restart(self, count: int, resumed: bool) -> None:
        self.restarts.append((count, resumed))

    def on_exit(self, code: int) -> None:
        self.exits.append(code)


@pytest.fixture
def make_spec(tmp_path: Path, fake_claude_bin: Path, scenario_file):
    log = tmp_path / "fake-invocations.jsonl"

    def make(
        scenario: dict | None = None,
        *,
        boot: dict | None = None,
        binary: str | None = None,
        max_restarts: int = 3,
        resume: bool = False,
        claude_session_id: str | None = None,
        hook_token: str = "hook-token-0123456789",
    ) -> SessionSpec:
        scenario = scenario or {"boot": {"bypass_dialog": True, "min_seconds": 0.1}}
        session_dir = tmp_path / "sessions" / "ses_0123456789ab"
        config_dir = session_dir / "config"
        workdir = tmp_path / "workdir" / "proj"
        config_dir.mkdir(parents=True, exist_ok=True)
        workdir.mkdir(parents=True, exist_ok=True)
        env = build_env(
            service_env=dict(os.environ),
            claude_env=ClaudeEnv(
                extra={
                    "FAKE_CLAUDE_SCENARIO": str(scenario_file(scenario)),
                    "FAKE_CLAUDE_LOG": str(log),
                }
            ),
            config_dir=str(config_dir),
            gitconfig=str(session_dir / "gitconfig"),
            session_id="ses_0123456789ab",
            internal_url="http://127.0.0.1:8090",
            hook_token=hook_token,
            gate_fail="allow",
            gate_timeout=14.0,
            mise_env={},
            passthrough=[],
        )
        return SessionSpec(
            session_id="ses_0123456789ab",
            claude_session_id=claude_session_id or str(uuid.uuid4()),
            workdir=str(workdir),
            config_dir=str(config_dir),
            session_dir=str(session_dir),
            binary=binary or str(fake_claude_bin),
            model="",
            effort="",
            extra_args=[],
            env=env,
            boot=BootConfig(**(boot or MARKER_ONLY)),
            max_restarts=max_restarts,
            resume=resume,
        )

    make.log = log  # type: ignore[attr-defined]
    return make


def invocations(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


@pytest.fixture
async def sessions():
    made: list[Session] = []

    def track(session: Session) -> Session:
        made.append(session)
        return session

    yield track
    for s in made:
        await s.stop(grace=1.0)


def script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


# ------------------------------------------------------------------ argv


def _spec(tmp_path: Path, **kw) -> SessionSpec:
    base = {
        "session_id": "ses_0123456789ab",
        "claude_session_id": "5b0c2a52-8a43-4a52-9a6e-0f1f3c3c2b11",
        "workdir": str(tmp_path / "wd"),
        "config_dir": str(tmp_path / "cfg"),
        "session_dir": str(tmp_path),
        "binary": "claude",
        "model": "opus",
        "effort": "high",
        "extra_args": ["--add-dir", "/data"],
        "env": {},
        "boot": BootConfig(),
        "max_restarts": 3,
        "resume": False,
    }
    base.update(kw)
    return SessionSpec(**base)


def test_argv_new_session_uses_session_id(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    assert build_argv(spec) == [
        "claude",
        "--model",
        "opus",
        "--effort",
        "high",
        "--session-id",
        spec.claude_session_id,
        "--add-dir",
        "/data",
    ]
    bare = _spec(tmp_path, model="", effort="", extra_args=[])
    assert build_argv(bare) == ["claude", "--session-id", spec.claude_session_id]


def test_argv_resume_uses_resume(tmp_path: Path) -> None:
    spec = _spec(tmp_path, resume=True)
    argv = build_argv(spec)
    assert argv[argv.index("--resume") + 1] == spec.claude_session_id
    assert "--session-id" not in argv
    # An existing transcript also means resume, even when the spec does not ask for it.
    fresh = _spec(tmp_path)
    transcript = (
        Path(fresh.config_dir) / "projects" / "workdir" / f"{fresh.claude_session_id}.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    argv = build_argv(fresh)
    assert argv[argv.index("--resume") + 1] == fresh.claude_session_id
    assert "--session-id" not in argv


def test_argv_rejects_dangerous_flag(tmp_path: Path) -> None:
    for flag in ("--dangerously-skip-permissions", "--dangerously-skip-permissions=true"):
        with pytest.raises(ValueError, match="dangerously-skip-permissions"):
            build_argv(_spec(tmp_path, extra_args=["--verbose", flag]))


# ------------------------------------------------------------------ env

SERVICE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/home/agent",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "USER": "agent",
    "SHELL": "/bin/bash",
    "TMPDIR": "/tmp",
    "CCW_AUTH_TOKEN": "service-bearer-secret-123",
    "CCW_CONFIG": "/etc/ccw/config.yaml",
    "SERVER_CALLBACK_SECRET": "callback-secret-xyz",
    "AWS_SECRET_ACCESS_KEY": "aws-secret-abcdef",
    "GITHUB_TOKEN": "ghp_service_token",
    "MY_TOOL_HOME": "/opt/tool",
    "PYTHONPATH": "/srv/app",
}


def _env(**kw) -> dict[str, str]:
    base = {
        "service_env": SERVICE_ENV,
        "claude_env": ClaudeEnv(
            claude_code_oauth_token="oauth-abcdef123456", disable_telemetry=True
        ),
        "config_dir": "/sessions/ses_1/config",
        "gitconfig": "/sessions/ses_1/gitconfig",
        "session_id": "ses_1",
        "internal_url": "http://127.0.0.1:8090",
        "hook_token": "hook-token-abcdef",
        "gate_fail": "deny",
        "gate_timeout": 14.0,
        "mise_env": {"PATH": "/mise/shims:/usr/bin", "MISE_DATA_DIR": "/mise"},
        "passthrough": ["MY_TOOL_HOME", "NOT_SET_ANYWHERE"],
    }
    base.update(kw)
    return build_env(**base)


def test_env_minimal_and_managed_vars() -> None:
    env = _env()
    assert env == {
        "PATH": "/mise/shims:/usr/bin",
        "HOME": "/home/agent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "USER": "agent",
        "SHELL": "/bin/bash",
        "TMPDIR": "/tmp",
        "TERM": "xterm-256color",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-abcdef123456",
        "DISABLE_TELEMETRY": "1",
        "CLAUDE_CONFIG_DIR": "/sessions/ses_1/config",
        "CLAUDE_CODE_PROJECT_DIR_NAME": "workdir",
        "GIT_CONFIG_GLOBAL": "/sessions/ses_1/gitconfig",
        "CCW_SESSION_ID": "ses_1",
        "CCW_INTERNAL_URL": "http://127.0.0.1:8090",
        "CCW_HOOK_TOKEN": "hook-token-abcdef",
        "CCW_TOOL_GATE_FAIL": "deny",
        "CCW_GATE_TIMEOUT": "14",
        "MISE_DATA_DIR": "/mise",
        "MY_TOOL_HOME": "/opt/tool",
    }
    # Base vars are copied only when present; the gate mode is validated.
    sparse = _env(service_env={"PATH": "/bin"}, mise_env=None, passthrough=[], gate_fail="allow")
    assert sparse["PATH"] == "/bin"
    assert "HOME" not in sparse and "TMPDIR" not in sparse
    assert sparse["CCW_TOOL_GATE_FAIL"] == "allow"
    with pytest.raises(ValueError, match="gate_fail"):
        _env(gate_fail="maybe")


def test_env_excludes_service_secrets() -> None:
    env = _env(
        # Even when listed for passthrough, CCW_* service vars and managed names stay out.
        passthrough=["CCW_AUTH_TOKEN", "CCW_CONFIG", "CLAUDE_CONFIG_DIR", "MY_TOOL_HOME"],
        service_env={**SERVICE_ENV, "CLAUDE_CONFIG_DIR": "/root/.claude"},
    )
    for name in (
        "CCW_AUTH_TOKEN",
        "CCW_CONFIG",
        "SERVER_CALLBACK_SECRET",
        "AWS_SECRET_ACCESS_KEY",
        "GITHUB_TOKEN",
        "PYTHONPATH",
    ):
        assert name not in env
    for secret in ("service-bearer-secret-123", "callback-secret-xyz", "aws-secret-abcdef"):
        assert secret not in env.values()
    assert env["CLAUDE_CONFIG_DIR"] == "/sessions/ses_1/config"
    assert env["MY_TOOL_HOME"] == "/opt/tool"


# ------------------------------------------------------------------ listener and vocabulary


def test_listener_base_noop_and_bind(tmp_path: Path) -> None:
    base = SessionListener()
    assert base.bind(object()) is None  # type: ignore[arg-type]
    assert base.on_state("ready") is None
    assert base.on_output("chunk") is None
    assert base.on_fatal("API Error: 401", "tail") is None
    assert base.on_restart(1, True) is None
    assert base.on_exit(1) is None
    assert base.on_stall("turn-1") is None
    assert base.on_turn_end(object()) is None

    rec = Recorder()
    store = object()
    session = Session(_spec(tmp_path), rec, turn_store=store)
    assert rec.bound is session
    assert session.listener is rec
    assert session.turn_store is store
    # A listener that only overrides some callbacks still works with the base defaults.
    Session(_spec(tmp_path), SessionListener())


async def test_snapshot_state_vocabulary(make_spec, sessions) -> None:
    assert PUBLIC_STATES == ("creating", "ready", "busy", "stopped", "failed", "dead")
    mapping = {
        "booting": "creating",
        "ready": "ready",
        "busy": "busy",
        "stopping": "stopped",
        "stopped": "stopped",
        "dead": "dead",
    }
    for internal, public in mapping.items():
        assert public_state(internal) == public

    rec = Recorder()
    session = sessions(Session(make_spec(), rec))
    snap = session.snapshot()
    assert snap["state"] == "creating"
    assert set(snap) >= {"state", "pid", "restarts", "last_fatal", "started_at", "ready_at"}
    await session.start()
    snap = session.snapshot()
    assert snap["state"] == "ready"
    assert snap["pid"] == session.pty.pid
    assert snap["restarts"] == 0 and snap["last_fatal"] is None
    assert snap["started_at"] and snap["ready_at"]
    session._set_state("busy")
    assert session.snapshot()["state"] == "busy"
    await session.stop(grace=1.0)
    assert session.snapshot()["state"] == "stopped"
    assert session.snapshot()["pid"] is None
    assert all(s in PUBLIC_STATES for s in rec.states)
    assert rec.states == ["creating", "ready", "busy", "stopped"]


# ------------------------------------------------------------------ boot


async def test_boot_accepts_bypass_dialog_and_becomes_ready(make_spec, sessions) -> None:
    rec = Recorder()
    spec = make_spec()
    session = sessions(Session(spec, rec))
    await session.start()
    assert session.state == "ready"
    assert session.pty.alive  # CR without Down would have exited the fake
    assert session.ring.contains("? for shortcuts")
    assert session.ring.contains("Bypass Permissions mode")
    assert rec.states == ["creating", "ready"]
    assert rec.output and "Fake Claude" in "".join(rec.output)
    assert session.transcript_path == os.path.join(
        spec.config_dir, "projects", "workdir", f"{spec.claude_session_id}.jsonl"
    )
    calls = invocations(make_spec.log)
    assert len(calls) == 1
    argv = calls[0]["argv"]
    assert argv[argv.index("--session-id") + 1] == spec.claude_session_id
    assert calls[0]["cwd"] == os.path.realpath(spec.workdir)
    assert calls[0]["env"]["CLAUDE_CONFIG_DIR"] == spec.config_dir
    assert calls[0]["env"]["CLAUDE_CODE_PROJECT_DIR_NAME"] == "workdir"
    assert calls[0]["env"]["CCW_SESSION_ID"] == spec.session_id


async def test_boot_ready_on_idle_without_marker(make_spec, sessions) -> None:
    spec = make_spec(
        {"boot": {"bypass_dialog": False, "min_seconds": 0.05}},
        boot={**FAST, "ready_markers": [], "dialogs": []},
    )
    session = sessions(Session(spec, Recorder()))
    started = time.monotonic()
    await session.start()
    assert session.state == "ready"
    assert time.monotonic() - started >= 0.2  # min_boot_seconds holds even when quiet


async def test_boot_timeout_relaunches_once_then_dead(make_spec, sessions) -> None:
    # No dialog handler and no marker the fake ever prints: the dialog sits unanswered.
    spec = make_spec(
        boot={**FAST, "timeout_seconds": 1.0, "idle_ready_seconds": 30.0, "dialogs": []}
    )
    rec = Recorder()
    session = sessions(Session(spec, rec))
    with pytest.raises(BootError) as info:
        await session.start()
    assert info.value.code == "boot_timeout"
    assert "Bypass Permissions mode" in info.value.tail
    assert len(info.value.tail) <= 800
    assert session.state == "dead"
    assert session.snapshot()["state"] == "dead"
    assert len(invocations(make_spec.log)) == 2
    assert rec.states == ["creating", "dead"]
    assert gone(session.pty.pid)


async def test_boot_timeout_tail_is_redacted(tmp_path: Path, make_spec, sessions) -> None:
    binary = script(
        tmp_path,
        "leaky",
        'printf "token=$CCW_HOOK_TOKEN key=sk-ant-abcdefghijklmnop\\r\\n"\nexec sleep 30\n',
    )
    spec = make_spec(
        binary=binary,
        hook_token="hook-secret-value-42",
        boot={**FAST, "timeout_seconds": 0.6, "idle_ready_seconds": 30.0, "dialogs": []},
    )
    session = sessions(Session(spec, Recorder()))
    with pytest.raises(BootError) as info:
        await session.start()
    assert "token=***" in info.value.tail
    assert "hook-secret-value-42" not in info.value.tail
    assert "sk-ant-abcdefghijklmnop" not in info.value.tail


async def test_boot_exit_relaunches_once_then_dead(tmp_path: Path, make_spec, sessions) -> None:
    count = tmp_path / "count"
    binary = script(tmp_path, "dies", f'echo run >> "{count}"\necho "boom"\nexit 7\n')
    session = sessions(Session(make_spec(binary=binary), Recorder()))
    with pytest.raises(BootError) as info:
        await session.start()
    assert info.value.code == "boot_exited"
    assert "boom" in info.value.tail
    assert session.state == "dead"
    assert count.read_text().count("run") == 2


async def test_spawn_failure_marks_dead(tmp_path: Path, make_spec, sessions) -> None:
    rec = Recorder()
    session = sessions(Session(make_spec(binary=str(tmp_path / "no-such-claude")), rec))
    with pytest.raises(BootError) as info:
        await session.start()
    assert info.value.code == "spawn_failed"
    assert session.state == "dead"
    assert rec.states == ["creating", "dead"]


async def test_stop_during_boot_aborts_start(make_spec, sessions) -> None:
    spec = make_spec(
        boot={**FAST, "timeout_seconds": 30.0, "idle_ready_seconds": 30.0, "dialogs": []}
    )
    session = sessions(Session(spec, Recorder()))
    task = asyncio.ensure_future(session.start())
    await wait_until(lambda: session.pty is not None and session.ring.contains("Fake Claude"))
    await session.stop(grace=1.0)
    with pytest.raises(BootError) as info:
        await task
    assert info.value.code == "stopped"
    assert session.state == "stopped"


# ------------------------------------------------------------------ crash and relaunch


async def test_crash_relaunches_with_resume_when_transcript_exists(make_spec, sessions) -> None:
    rec = Recorder()
    spec = make_spec(CRASH_SCENARIO)
    session = sessions(Session(spec, rec))
    await session.start()
    first_pid = session.pty.pid
    await session.pty.send_prompt("crash now", 0.2)
    await wait_until(lambda: rec.restarts, timeout=15)
    assert rec.restarts == [(1, True)]
    assert session.state == "ready"
    assert session.restarts == 1
    assert session.pty.pid != first_pid and session.pty.alive
    assert os.path.isfile(session.transcript_path)
    calls = invocations(make_spec.log)
    assert len(calls) == 2
    assert "--session-id" in calls[0]["argv"]
    second = calls[1]["argv"]
    assert second[second.index("--resume") + 1] == spec.claude_session_id
    assert "--session-id" not in second
    assert rec.states == ["creating", "ready", "creating", "ready"]
    assert rec.exits == []
    assert session.snapshot()["restarts"] == 1


async def test_crash_without_transcript_relaunches_with_session_id(make_spec, sessions) -> None:
    rec = Recorder()
    spec = make_spec()
    session = sessions(Session(spec, rec))
    await session.start()
    await session.pty.write(b"\x03")  # the fake exits 0 at idle; nothing was written to disk
    await wait_until(lambda: rec.restarts, timeout=15)
    assert rec.restarts == [(1, False)]
    assert not os.path.exists(session.transcript_path)
    calls = invocations(make_spec.log)
    assert len(calls) == 2
    second = calls[1]["argv"]
    assert second[second.index("--session-id") + 1] == spec.claude_session_id
    assert "--resume" not in second


async def test_restart_budget_exhausted_dead(make_spec, sessions) -> None:
    rec = Recorder()
    session = sessions(Session(make_spec(CRASH_SCENARIO, max_restarts=1), rec))
    await session.start()
    await session.pty.send_prompt("crash one", 0.2)
    await wait_until(lambda: rec.restarts, timeout=15)
    await session.pty.send_prompt("crash two", 0.2)
    # state turns dead the moment claude exits; on_exit fires only after the pty close grace.
    await wait_until(lambda: session.state == "dead" and rec.exits, timeout=15)
    assert rec.restarts == [(1, True)]
    assert rec.exits == [3]
    assert session.restarts == 1
    assert session.snapshot()["state"] == "dead"
    assert rec.states[-1] == "dead"
    await asyncio.sleep(0.5)
    assert len(invocations(make_spec.log)) == 2  # no relaunch past the budget


async def test_ring_reset_on_relaunch(make_spec, sessions) -> None:
    # The first claude prints a dialog prompt as turn output, then crashes. If that stale text
    # were still in the ring on relaunch, the trust handler would send CR while the new claude is
    # still painting, and the fake exits on early input.
    scenario = {
        "boot": {"bypass_dialog": True, "min_seconds": 0.3},
        "turns": [
            {
                "match": "crash",
                "steps": [{"text": "Do you trust the files in this folder?"}, {"exit": 3}],
            }
        ],
    }
    trust = BootDialog(pattern="Do you trust the files in this folder", keys=["\r"])
    rec = Recorder()
    session = sessions(
        Session(make_spec(scenario, boot={**MARKER_ONLY, "dialogs": [BYPASS, trust]}), rec)
    )
    await session.start()
    await session.pty.send_prompt("crash", 0.2)
    await wait_until(lambda: rec.restarts or session.state == "dead", timeout=15)
    assert rec.restarts == [(1, True)]
    assert session.state == "ready"
    assert rec.ring_at_creating[-1] == ""
    assert not session.ring.contains("Do you trust the files in this folder")
    assert len(invocations(make_spec.log)) == 2


# ------------------------------------------------------------------ fatal and stop


async def test_fatal_pattern_reported(make_spec, sessions) -> None:
    scenario = {
        "boot": {"bypass_dialog": True, "min_seconds": 0.1},
        "turns": [
            {
                "match": "first",
                "steps": [{"print": "\x1b[31mapi \x1b[1merror:\x1b[0m 401 bad key"}],
            },
            {"match": "second", "steps": [{"print": "API Error: 401 again"}]},
        ],
    }
    rec = Recorder()
    session = sessions(Session(make_spec(scenario), rec))
    await session.start()
    await session.pty.send_prompt("first", 0.2)
    await wait_until(lambda: rec.fatals)
    assert rec.fatals[0][0] == "API Error: 401"
    assert "401 bad key" in rec.fatals[0][1]
    assert session.last_fatal == "API Error: 401"
    assert session.snapshot()["last_fatal"] == "API Error: 401"
    await asyncio.sleep(0.3)
    assert len(rec.fatals) == 1  # once per occurrence, not once per later chunk
    await session.pty.send_prompt("second", 0.2)
    await wait_until(lambda: len(rec.fatals) == 2)
    await session.pty.send_prompt("an ordinary prompt", 0.2)
    await wait_until(lambda: session.ring.contains("echo: an ordinary prompt"))
    await asyncio.sleep(0.2)
    assert len(rec.fatals) == 2
    assert session.state == "ready"


async def test_stop_kills_process(make_spec, sessions) -> None:
    rec = Recorder()
    session = sessions(Session(make_spec(), rec))
    await session.start()
    pid = session.pty.pid
    await session.stop(grace=1.0)
    assert session.state == "stopped"
    assert gone(pid)
    await asyncio.sleep(0.5)
    assert rec.restarts == [] and rec.exits == []
    assert len(invocations(make_spec.log)) == 1
    assert rec.states == ["creating", "ready", "stopped"]
    await session.stop(grace=1.0)  # idempotent
    assert session.state == "stopped"


def test_fatal_scanner_matches_across_chunks() -> None:
    scanner = FatalScanner(["API Error: 401", "Please run /login"])
    assert scanner.feed("noise API Er") == []
    assert scanner.feed("ror:\x1b[3") == []  # escape cut at the chunk end is held back
    assert scanner.feed("1m 401 then please RUN /login") == ["API Error: 401", "Please run /login"]
    assert scanner.feed(" more output") == []  # already reported, not again
    assert scanner.feed("API Error: 401 API Error: 401") == ["API Error: 401", "API Error: 401"]
    scanner.reset()
    assert scanner.feed("Error: 401") == []  # reset drops the carry
