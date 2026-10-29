"""SessionManager (D4, D2, D12, D14, D11, D19, D20, D24) against real parts and the fake claude.

Every test runs the real bootstrap, workdir, plugin, settings, session and router code with the
scripted fake claude as `claude.binary`. A small loopback HTTP server stands in for the internal
API (it hands each relayed Claude hook to `manager.router(id).handle_claude_hook`, after checking
the hook token) and records callback POSTs, so Stop hooks complete turns end to end.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import World, jsonl, turns_of, wait_turn, wait_until

from claude_code_wrapper import manager as manager_mod
from claude_code_wrapper.bootstrap.settings import relay_timeouts
from claude_code_wrapper.claude_env import ClaudeEnv
from claude_code_wrapper.events import EventLog
from claude_code_wrapper.manager import (
    CreateFailed,
    CreateSessionRequest,
    CreateVetoed,
    InvalidRequest,
    InvalidState,
    ResumeUnavailable,
    ResumeVetoed,
    SessionNotFound,
    TooManySessions,
)
from claude_code_wrapper.redact import Redactor
from claude_code_wrapper.session import PUBLIC_STATES, Session
from claude_code_wrapper.turns import TurnStore

SECRET_TOKEN = "sk-ant-oat01-overridesecret0123456789"
SECRET_EXTRA = "extra-secret-value-0123456789"


def record_hook(path: Path, **kw: Any) -> dict:
    return {"type": "command", "run": f"cat >> '{path}'; echo >> '{path}'", **kw}


def deny_hook(reason: str) -> dict:
    return {
        "type": "command",
        "run": f"cat > /dev/null; echo '{reason}' >&2; exit 2",
        "blocking": True,
    }


def pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def marketplace(root: Path, name: str = "wb") -> Path:
    d = root / name
    (d / ".claude-plugin").mkdir(parents=True)
    (d / ".claude-plugin" / "marketplace.json").write_text(json.dumps({"name": name}))
    return d


# ------------------------------------------------------------------ create


STEP_FUNCS = (
    "prepare_workdir",
    "write_gitconfig",
    "run_setup_commands",
    "install_workdir",
    "ensure_config_dir",
    "seed_claude_json",
    "write_instructions",
    "copy_config_files",
    "install_plugins",
    "render_settings",
    "build_env",
)


async def test_create_runs_steps_in_order_and_is_ready(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hooks_out = tmp_path / "on_create.jsonl"
    source = tmp_path / "rules-src"
    source.mkdir()
    (source / "rule.md").write_text("be nice")
    cfg = world.cfg(
        init={
            "git": {"user_name": "Agent", "user_email": "agent@example.com"},
            "setup_commands": ["echo setup > setup.txt"],
        },
        claude={"config_files": [{"source": str(source), "target": "rules"}]},
        prompts={"system_append": "Be terse."},
        plugins={
            "marketplaces": [{"name": "wb", "source": str(marketplace(tmp_path))}],
            "install": [{"plugin": "workbench@wb"}],
        },
        hooks={"onCreate": [record_hook(hooks_out)]},
    )
    m = world.manager(cfg)
    calls: list[tuple[str, str | None, str | None]] = []

    def current_meta() -> dict:
        found = list((tmp_path / "sessions").glob("*/session.json"))
        return json.loads(found[0].read_text()) if found else {}

    def wrap(name: str, fn: Any) -> Any:
        if inspect.iscoroutinefunction(fn):

            async def async_wrapper(*a: Any, **kw: Any) -> Any:
                meta = current_meta()
                calls.append((name, meta.get("state"), meta.get("step")))
                return await fn(*a, **kw)

            return async_wrapper

        def wrapper(*a: Any, **kw: Any) -> Any:
            meta = current_meta()
            calls.append((name, meta.get("state"), meta.get("step")))
            return fn(*a, **kw)

        return wrapper

    for name in STEP_FUNCS:
        monkeypatch.setattr(manager_mod, name, wrap(name, getattr(manager_mod, name)))
    monkeypatch.setattr(Session, "start", wrap("start", Session.start))

    snap = await m.create(CreateSessionRequest(name="first"))

    assert [c[0] for c in calls] == [*STEP_FUNCS, "start"]
    expected_steps = {"build_env": "child_env", "start": "start"}
    for name, state, step in calls:
        assert state == "creating"
        assert step == expected_steps.get(name, name)

    sid = snap["session_id"]
    assert sid.startswith("ses_") and len(sid) == 16
    int(sid[4:], 16)
    assert snap["state"] == "ready"
    assert snap["name"] == "first"
    assert snap["workdir_mode"] == "empty"
    workdir = Path(snap["workdir"])
    assert workdir == tmp_path / "workdir" / sid
    assert (workdir / "setup.txt").read_text() == "setup\n"
    sdir = world.session_dir(sid)
    for rel in ("setup.done", "gitconfig", "config/.claude.json", "config/settings.json"):
        assert (sdir / rel).exists(), rel
    assert "Be terse." in (sdir / "config" / "CLAUDE.md").read_text()
    assert (sdir / "config" / "rules" / "rule.md").read_text() == "be nice"
    state = json.loads((sdir / "config" / "plugins" / "fake-state.json").read_text())
    assert "workbench@wb" in json.dumps(state)
    assert "Agent" in (sdir / "gitconfig").read_text()

    meta = world.meta(sid)
    assert meta["state"] == "ready"
    assert meta["claude_session_id"] == snap["claude_session_id"]
    assert set(meta) >= {
        "session_id",
        "claude_session_id",
        "name",
        "workdir",
        "workdir_mode",
        "model",
        "effort",
        "env",
        "callbackUrl",
        "created_at",
        "updated_at",
        "state",
        "step",
        "error",
        "last_activity_at",
    }

    await wait_until(lambda: jsonl(hooks_out))  # non-blocking: fired in the background
    created = jsonl(hooks_out)
    assert len(created) == 1
    assert created[0]["event"] == "onCreate"
    assert created[0]["session_id"] == sid
    assert created[0]["data"]["session_id"] == sid

    router = m.router(sid)
    token = router.session.spec.env["CCW_HOOK_TOKEN"]
    assert len(token) >= 32
    assert m.hook_token_matches(sid, token)
    assert not m.hook_token_matches(sid, token + "x")
    assert not m.hook_token_matches(sid, None)
    assert not m.hook_token_matches("ses_000000000000", token)
    assert router.session.spec.env["CCW_INTERNAL_URL"] == world.url
    assert m.get(sid)["state"] == "ready"
    assert [s["session_id"] for s in m.list()] == [sid]
    with pytest.raises(SessionNotFound):
        m.get("ses_ffffffffffff")
    with pytest.raises(SessionNotFound):
        m.get("../etc")


async def test_max_sessions_enforced(world: World) -> None:
    m = world.manager(world.cfg(sessions={"max_sessions": 1}))
    results = await asyncio.gather(
        m.create(CreateSessionRequest()), m.create(CreateSessionRequest()), return_exceptions=True
    )
    assert sum(isinstance(r, TooManySessions) for r in results) == 1
    first = next(r for r in results if isinstance(r, dict))
    with pytest.raises(TooManySessions):
        await m.create(CreateSessionRequest())
    await m.destroy(first["session_id"])
    again = await m.create(CreateSessionRequest())
    assert again["state"] == "ready"


async def test_create_failure_marks_failed_and_fires_on_error(world: World, tmp_path: Path) -> None:
    errors = tmp_path / "on_error.jsonl"
    cfg = world.cfg(
        init={"setup_commands": ['echo "token=$MY_API_TOKEN"; exit 3']},
        hooks={"onError": [record_hook(errors, blocking=True)]},
    )
    m = world.manager(cfg)
    req = CreateSessionRequest(env=ClaudeEnv(extra={"MY_API_TOKEN": SECRET_EXTRA}))
    with pytest.raises(CreateFailed) as exc:
        await m.create(req)
    assert exc.value.step == "run_setup_commands"
    assert SECRET_EXTRA not in exc.value.message
    assert "***" in exc.value.message

    sid = next((tmp_path / "sessions").iterdir()).name
    meta = world.meta(sid)
    assert meta["state"] == "failed"
    assert meta["step"] == "run_setup_commands"
    assert meta["error"]["step"] == "run_setup_commands"
    assert "exit code 3" in meta["error"]["message"]
    assert SECRET_EXTRA not in (world.session_dir(sid) / "session.json").read_text()
    error_events = [e for e in world.events(sid) if e["type"] == "error"]
    assert error_events and error_events[-1]["data"]["kind"] == "create_failed"
    assert error_events[-1]["data"]["step"] == "run_setup_commands"
    assert SECRET_EXTRA not in (world.session_dir(sid) / "events.jsonl").read_text()
    fired = jsonl(errors)
    assert fired[-1]["event"] == "onError"
    assert fired[-1]["data"]["kind"] == "create_failed"
    assert fired[-1]["data"]["step"] == "run_setup_commands"
    assert m.get(sid)["state"] == "failed"
    assert m.list(state="failed")[0]["session_id"] == sid
    with pytest.raises(InvalidState):
        m.router(sid)
    # The failed session freed its slot and is not resumable (claude never started).
    with pytest.raises(ResumeUnavailable):
        await m.resume(sid)

    # A failure after claude started (it never boots) stops claude and names the step.
    dead_bin = tmp_path / "dead-claude"
    dead_bin.write_text("#!/bin/sh\nexit 1\n")
    dead_bin.chmod(0o755)
    m2 = world.manager(world.cfg(claude={"binary": str(dead_bin)}))
    with pytest.raises(CreateFailed) as exc:
        await m2.create(CreateSessionRequest())
    assert exc.value.step == "start"
    failed = [s for s in m2.list(state="failed") if s["session_id"] != sid]
    assert failed and failed[0]["error"]["step"] == "start"
    assert m2.list(state="creating") == []


async def test_on_create_veto_destroys(world: World) -> None:
    m = world.manager(world.cfg(hooks={"onCreate": [deny_hook("not today")]}))
    with pytest.raises(CreateVetoed) as exc:
        await m.create(CreateSessionRequest())
    assert exc.value.reason == "not today"
    [snap] = m.list()
    assert snap["state"] == "stopped"
    assert world.meta(snap["session_id"])["state"] == "stopped"
    argv = world.interactive_argvs()
    assert len(argv) == 1  # claude was started, then stopped
    with pytest.raises(InvalidState):
        m.router(snap["session_id"])
    # The vetoed session does not hold a slot.
    assert not m.active_ids()


# ------------------------------------------------------------------ initial prompt


async def test_initial_prompt_submitted(world: World) -> None:
    cfg = world.cfg(
        prompts={
            "templates": {
                "greet": {"text": "Hello {{who}}", "variables": {"who": {"required": True}}}
            },
            "initial_prompt": {"template": "greet", "variables": {"who": "bob"}},
        }
    )
    m = world.manager(cfg)
    snap = await m.create(CreateSessionRequest())
    turn = await wait_turn(m, snap["session_id"])
    assert turn.origin == "initial"
    assert turn.text == "Hello bob"
    assert turn.final_text == "echo: Hello bob" + f"\n\n<!-- ccw turn: {turn.turn_id} -->"

    # A request prompt replaces the configured one; a request template works too.
    snap2 = await m.create(CreateSessionRequest(prompt="custom start"))
    turn2 = await wait_turn(m, snap2["session_id"])
    assert (turn2.origin, turn2.text) == ("initial", "custom start")
    snap3 = await m.create(CreateSessionRequest(template="greet", variables={"who": "eve"}))
    turn3 = await wait_turn(m, snap3["session_id"])
    assert (turn3.origin, turn3.text) == ("initial", "Hello eve")


async def test_create_does_not_wait_for_initial_turn(world: World) -> None:
    scenario = {"turns": [{"match": "slow", "steps": [{"sleep": 2.5}, {"text": "done"}]}]}
    m = world.manager(world.cfg(scenario))
    started = time.monotonic()
    snap = await m.create(CreateSessionRequest(prompt="slow please"))
    assert snap["state"] == "ready"
    assert all(t.state != "completed" for t in turns_of(m, snap["session_id"]))
    await wait_until(lambda: m.get(snap["session_id"])["state"] == "busy")
    assert m.get(snap["session_id"])["current_turn_id"] is not None
    turn = await wait_turn(m, snap["session_id"])
    assert turn.final_text == "done"
    assert time.monotonic() - started >= 2.5


async def test_initial_turn_veto_keeps_session_ready(world: World) -> None:
    m = world.manager(world.cfg(hooks={"onTurn": [deny_hook("no initial turn")]}))
    snap = await m.create(CreateSessionRequest(prompt="hello"))
    sid = snap["session_id"]

    def rejected() -> list[dict]:
        return [
            e
            for e in world.events(sid)
            if e["type"] == "error" and e["data"].get("kind") == "initial_turn_rejected"
        ]

    await wait_until(rejected)
    assert rejected()[0]["data"]["reason"] == "no initial turn"
    assert m.get(sid)["state"] == "ready"
    assert turns_of(m, sid) == []


def initial_rejections(world: World, sid: str) -> list[dict]:
    return [
        e["data"]
        for e in world.events(sid)
        if e["type"] == "error" and e["data"].get("kind") == "initial_turn_rejected"
    ]


def fake_inputs(world: World, sid: str) -> list[str]:
    return [i["text"] for i in jsonl(world.session_dir(sid) / "config" / "fake-inputs.jsonl")]


async def test_initial_slash_prompt_runs_as_command(world: World) -> None:
    commands = {"client": ["*"], "quiet_seconds": 0.2}
    m = world.manager(world.cfg(sessions={"commands": commands}))
    sid = (await m.create(CreateSessionRequest(prompt="/reload-plugins")))["session_id"]
    router = m.router(sid)
    await world.until(lambda: router.commands.list(), what="the initial command record")
    (rec,) = router.commands.list()
    assert (rec.name, rec.args, rec.origin, rec.turn_id) == ("reload-plugins", "", "client", None)
    await world.until(lambda: rec.state == "completed", what="the initial command completing")
    await world.until(lambda: m.get(sid)["state"] == "ready", what="the session back to ready")
    assert turns_of(m, sid) == []
    assert fake_inputs(world, sid) == ["/reload-plugins"]
    assert initial_rejections(world, sid) == []


@pytest.mark.parametrize(
    ("client", "prompt", "reason"),
    [([], "/reload-plugins", "not_allowed"), (["*"], "/ is root", "invalid_command")],
)
async def test_initial_slash_prompt_refused_is_reported(
    world: World, client: list[str], prompt: str, reason: str
) -> None:
    m = world.manager(world.cfg(sessions={"commands": {"client": client}}))
    sid = (await m.create(CreateSessionRequest(prompt=prompt)))["session_id"]
    await world.until(lambda: initial_rejections(world, sid), what="initial_turn_rejected")
    (rejected,) = initial_rejections(world, sid)
    assert rejected["reason"] == reason
    assert rejected["message"].startswith("initial turn rejected: ")
    assert m.get(sid)["state"] == "ready"
    assert m.router(sid).commands.list() == []
    assert turns_of(m, sid) == []
    assert fake_inputs(world, sid) == []


# ------------------------------------------------------------------ env


async def test_env_override_merged_and_not_persisted_secret(world: World) -> None:
    redactor = Redactor()
    cfg = world.cfg(claude={"env": {"anthropic_model": "base-model", "extra": {"BASE_V": "b"}}})
    m = world.manager(cfg, redactor=redactor)
    override = ClaudeEnv(
        claude_code_oauth_token=SECRET_TOKEN,
        disable_telemetry=True,
        extra={"OVERRIDE_V": "o", "MY_API_TOKEN": SECRET_EXTRA},
    )
    snap = await m.create(CreateSessionRequest(env=override, model="opus-x", effort="high"))
    sid = snap["session_id"]
    env = m.router(sid).session.spec.env
    assert env["ANTHROPIC_MODEL"] == "base-model"
    assert env["BASE_V"] == "b"
    assert env["OVERRIDE_V"] == "o"
    assert env["DISABLE_TELEMETRY"] == "1"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == SECRET_TOKEN
    assert env["MY_API_TOKEN"] == SECRET_EXTRA
    argv = world.interactive_argvs()[-1]
    assert argv[argv.index("--model") + 1] == "opus-x"
    assert argv[argv.index("--effort") + 1] == "high"
    assert snap["model"] == "opus-x" and snap["effort"] == "high"

    raw = (world.session_dir(sid) / "session.json").read_text()
    assert SECRET_TOKEN not in raw and SECRET_EXTRA not in raw
    meta = json.loads(raw)
    assert meta["env"] == {"disable_telemetry": True, "extra": {"OVERRIDE_V": "o"}}
    assert snap["env"]["claude_code_oauth_token"] == "***"
    assert snap["env"]["extra"]["MY_API_TOKEN"] == "***"
    assert snap["env"]["anthropic_model"] == "base-model"
    assert SECRET_TOKEN not in json.dumps(snap) and SECRET_EXTRA not in json.dumps(snap)
    # The shared redactor learned every secret of the merged env.
    assert redactor.redact(f"x {SECRET_TOKEN} {SECRET_EXTRA}") == "x *** ***"
    for path in world.session_dir(sid).rglob("*"):
        if path.is_file() and path.suffix in (".json", ".jsonl", "") and path.stat().st_size:
            assert SECRET_EXTRA not in path.read_text(errors="replace"), path


async def test_invalid_merged_env_rejected_before_anything(world: World, tmp_path: Path) -> None:
    m = world.manager(world.cfg(claude={"env": {"claude_code_use_bedrock": True}}))
    with pytest.raises(InvalidRequest):
        await m.create(CreateSessionRequest(env=ClaudeEnv(claude_code_use_vertex=True)))
    assert not (tmp_path / "sessions").exists() or not any((tmp_path / "sessions").iterdir())
    # false unsets the configured flag, so this override is valid.
    snap = await m.create(
        CreateSessionRequest(
            env=ClaudeEnv(claude_code_use_bedrock=False, claude_code_use_vertex=True)
        )
    )
    env = m.router(snap["session_id"]).session.spec.env
    assert env["CLAUDE_CODE_USE_VERTEX"] == "1" and "CLAUDE_CODE_USE_BEDROCK" not in env


async def test_gate_env_from_hooks(world: World) -> None:
    gated = world.cfg(
        hooks={
            "onAgentToolCall": [
                {
                    "type": "command",
                    "run": "true",
                    "blocking": True,
                    "on_error": "deny",
                    "timeout_seconds": 40,
                }
            ]
        }
    )
    m = world.manager(gated)
    snap = await m.create(CreateSessionRequest())
    env = m.router(snap["session_id"]).session.spec.env
    assert env["CCW_TOOL_GATE_FAIL"] == "deny"
    assert env["CCW_GATE_TIMEOUT"] == f"{relay_timeouts(gated).attempt_timeout:g}"
    assert relay_timeouts(gated).attempt_timeout == 21.5

    # A non-blocking deny-on-error action does not make the gate fail closed.
    lax = world.cfg(
        hooks={"onAgentToolCall": [{"type": "command", "run": "true", "on_error": "deny"}]}
    )
    m2 = world.manager(lax)
    snap2 = await m2.create(CreateSessionRequest())
    env2 = m2.router(snap2["session_id"]).session.spec.env
    assert env2["CCW_TOOL_GATE_FAIL"] == "allow"
    assert env2["CCW_GATE_TIMEOUT"] == f"{relay_timeouts(lax).attempt_timeout:g}"


# ------------------------------------------------------------------ destroy


async def test_destroy_fires_on_destroy_and_keeps_files(world: World, tmp_path: Path) -> None:
    out = tmp_path / "on_destroy.jsonl"
    m = world.manager(world.cfg(hooks={"onDestroy": [record_hook(out)]}))
    snap = await m.create(CreateSessionRequest(prompt="hi"))
    sid = snap["session_id"]
    await wait_turn(m, sid)
    pid = m.router(sid).session.snapshot()["pid"]
    assert pid_alive(pid)

    stopped = await m.destroy(sid)
    assert stopped["state"] == "stopped"
    assert not stopped.get("purged")
    fired = jsonl(out)
    assert [p["event"] for p in fired] == ["onDestroy"]
    assert fired[0]["session_id"] == sid
    assert fired[0]["data"]["state"] == "stopped"
    await wait_until(lambda: not pid_alive(pid), 5)

    assert world.session_dir(sid).is_dir()
    assert Path(snap["workdir"]).is_dir()
    assert world.meta(sid)["state"] == "stopped"
    assert m.get(sid)["state"] == "stopped"
    assert m.get(sid)["turns_completed"] == 1
    assert world.events(sid)[-1]["type"] in ("session_closed", "hook_result")
    assert any(e["type"] == "session_closed" for e in world.events(sid))
    # Destroying again is harmless and does not fire onDestroy twice.
    assert (await m.destroy(sid))["state"] == "stopped"
    assert len(jsonl(out)) == 1
    with pytest.raises(SessionNotFound):
        await m.destroy("ses_ffffffffffff")


async def test_purge_removes_session_dir_not_mounted_workdir(world: World, tmp_path: Path) -> None:
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    (mounted / "keep.txt").write_text("precious")
    m = world.manager(world.cfg(init={"workdir": {"mode": "mounted", "path": str(mounted)}}))
    a = await m.create(CreateSessionRequest())
    b = await m.create(CreateSessionRequest())  # two sessions share one mounted workdir
    assert a["workdir"] == b["workdir"] == str(mounted)
    assert a["workdir_mode"] == "mounted"
    purged = await m.destroy(a["session_id"], purge=True)
    assert purged["purged"] is True and purged["state"] == "stopped"
    assert not world.session_dir(a["session_id"]).exists()
    assert (mounted / "keep.txt").read_text() == "precious"
    with pytest.raises(SessionNotFound):
        m.get(a["session_id"])
    assert m.get(b["session_id"])["state"] == "ready"

    # The request may name another mounted path; it is never deleted either.
    other = tmp_path / "other-mount"
    other.mkdir()
    c = await m.create(CreateSessionRequest(workdir=str(other)))
    assert c["workdir"] == str(other)
    await m.destroy(c["session_id"])
    await m.destroy(c["session_id"], purge=True)  # purge of an already stopped session
    assert other.is_dir() and not world.session_dir(c["session_id"]).exists()

    # Mode empty: purge deletes the session's own workdir.
    m2 = world.manager(world.cfg())
    e = await m2.create(CreateSessionRequest(workdir="scratch"))
    assert Path(e["workdir"]) == tmp_path / "workdir" / "scratch"
    (Path(e["workdir"]) / "f.txt").write_text("x")
    await m2.destroy(e["session_id"], purge=True)
    assert not Path(e["workdir"]).exists()
    assert not world.session_dir(e["session_id"]).exists()


# ------------------------------------------------------------------ resume


async def test_resume_uses_resume_flag_and_fires_on_resume(world: World, tmp_path: Path) -> None:
    out = tmp_path / "on_resume.jsonl"
    m = world.manager(world.cfg(hooks={"onResume": [record_hook(out)]}))
    snap = await m.create(CreateSessionRequest(prompt="remember me"))
    sid, csid = snap["session_id"], snap["claude_session_id"]
    await wait_turn(m, sid)
    with pytest.raises(InvalidState):
        await m.resume(sid)  # still running
    await m.destroy(sid)

    resumed = await m.resume(sid)
    assert resumed["state"] == "ready"
    assert resumed["claude_session_id"] == csid
    argv = world.interactive_argvs()[-1]
    assert argv[argv.index("--resume") + 1] == csid
    assert "--session-id" not in argv
    await wait_until(lambda: jsonl(out))  # non-blocking: fired in the background
    fired = jsonl(out)
    assert [p["event"] for p in fired] == ["onResume"]
    assert fired[0]["data"]["state"] == "ready"
    assert world.meta(sid)["state"] == "ready"
    # The resumed session takes turns again; its history continues in the same files.
    turn = await m.router(sid).submit_turn("again")
    await wait_until(lambda: m.open_turns(sid).get(turn.turn_id).state == "completed")
    assert len(turns_of(m, sid)) == 2


async def test_resume_veto_leaves_session_stopped(world: World) -> None:
    m = world.manager(world.cfg(hooks={"onResume": [deny_hook("stay down")]}))
    snap = await m.create(CreateSessionRequest(prompt="hi"))
    sid = snap["session_id"]
    await wait_turn(m, sid)
    await m.destroy(sid)
    with pytest.raises(ResumeVetoed) as exc:
        await m.resume(sid)
    assert exc.value.reason == "stay down"
    assert m.get(sid)["state"] == "stopped"
    assert not m.active_ids()


async def test_dead_session_is_released_and_resumable(world: World) -> None:
    scenario = {"turns": [{"match": "crash", "steps": [{"exit": 1}]}]}
    m = world.manager(world.cfg(scenario, sessions={"max_restarts": 0, "max_sessions": 1}))
    snap = await m.create(CreateSessionRequest(prompt="crash now"))
    sid = snap["session_id"]
    await wait_until(lambda: m.get(sid)["state"] == "dead")
    await wait_until(lambda: not m.active_ids())  # released: it holds no slot
    assert world.meta(sid)["state"] == "dead"
    [turn] = turns_of(m, sid)
    assert (turn.state, turn.error) == ("failed", "claude_exited")

    resumed = await m.resume(sid)
    assert resumed["state"] == "ready"
    argv = world.interactive_argvs()[-1]
    assert argv[argv.index("--resume") + 1] == snap["claude_session_id"]
    await m.destroy(sid)

    # Failed after claude had started: resumable; failed before: not.
    meta = world.meta(sid)
    meta.update(state="failed", claude_started=True)
    (world.session_dir(sid) / "session.json").write_text(json.dumps(meta))
    assert (await m.resume(sid))["state"] == "ready"
    await m.destroy(sid)
    meta = world.meta(sid)
    meta.update(state="failed", claude_started=False)
    (world.session_dir(sid) / "session.json").write_text(json.dumps(meta))
    with pytest.raises(ResumeUnavailable):
        await m.resume(sid)


async def test_resume_without_transcript_rejected(world: World) -> None:
    m = world.manager(world.cfg())
    snap = await m.create(CreateSessionRequest())
    sid = snap["session_id"]
    await m.destroy(sid)
    with pytest.raises(ResumeUnavailable):
        await m.resume(sid)
    assert m.get(sid)["state"] == "stopped"
    with pytest.raises(SessionNotFound):
        await m.resume("ses_ffffffffffff")


async def test_secret_override_must_be_resent_on_resume(world: World) -> None:
    m = world.manager(world.cfg())
    override = ClaudeEnv(extra={"MY_API_TOKEN": SECRET_EXTRA, "PLAIN_V": "kept"})
    snap = await m.create(CreateSessionRequest(env=override, prompt="hi"))
    sid = snap["session_id"]
    await wait_turn(m, sid)
    assert m.router(sid).session.spec.env["MY_API_TOKEN"] == SECRET_EXTRA
    await m.destroy(sid)

    await m.resume(sid)
    env = m.router(sid).session.spec.env
    assert "MY_API_TOKEN" not in env
    assert env["PLAIN_V"] == "kept"  # non-secret overrides survive
    await m.destroy(sid)

    resent = await m.resume(sid, env=ClaudeEnv(extra={"MY_API_TOKEN": SECRET_EXTRA}))
    env = m.router(sid).session.spec.env
    assert env["MY_API_TOKEN"] == SECRET_EXTRA
    assert env["PLAIN_V"] == "kept"
    assert resent["env"]["extra"]["MY_API_TOKEN"] == "***"
    assert SECRET_EXTRA not in (world.session_dir(sid) / "session.json").read_text()


# ------------------------------------------------------------------ persistence


async def test_list_includes_persisted_sessions_after_reload(world: World) -> None:
    cfg = world.cfg()
    m1 = world.manager(cfg)
    live = await m1.create(CreateSessionRequest(name="live", prompt="hi"))
    gone = await m1.create(CreateSessionRequest(name="gone"))
    await wait_turn(m1, live["session_id"])
    await m1.destroy(gone["session_id"])
    assert world.meta(live["session_id"])["state"] == "ready"

    # A new service process: the live session's claude died with the old one.
    m2 = world.manager(cfg)
    await m2.load_persisted()
    by_id = {s["session_id"]: s for s in m2.list()}
    assert set(by_id) == {live["session_id"], gone["session_id"]}
    assert by_id[live["session_id"]]["state"] == "stopped"
    assert by_id[live["session_id"]]["name"] == "live"
    assert by_id[live["session_id"]]["turns_completed"] == 1
    assert by_id[gone["session_id"]]["state"] == "stopped"
    assert world.meta(live["session_id"])["state"] == "stopped"
    assert {s["session_id"] for s in m2.list(state="stopped")} == set(by_id)
    assert m2.list(state="ready") == []
    assert m2.get(live["session_id"])["claude_session_id"] == live["claude_session_id"]
    assert not m2.started  # service-main sets it once listeners are bound


async def test_load_persisted_resumes_with_resume_on_start(world: World) -> None:
    m1 = world.manager(world.cfg())
    live = await m1.create(CreateSessionRequest(prompt="hi"))
    await wait_turn(m1, live["session_id"])
    fresh = await m1.create(CreateSessionRequest())  # no transcript: cannot resume
    await m1.shutdown()
    for sid in (live["session_id"], fresh["session_id"]):
        meta = world.meta(sid)
        meta["state"] = "busy"  # as if the old process died mid-turn
        (world.session_dir(sid) / "session.json").write_text(json.dumps(meta))

    m2 = world.manager(world.cfg(sessions={"resume_on_start": True}))
    await m2.load_persisted()
    assert m2.get(live["session_id"])["state"] == "ready"
    assert m2.get(fresh["session_id"])["state"] == "stopped"
    argv = world.interactive_argvs()[-1]
    assert argv[argv.index("--resume") + 1] == live["claude_session_id"]


async def test_load_persisted_resumes_interrupted_creating(world: World) -> None:
    m1 = world.manager(world.cfg())
    resuming = await m1.create(CreateSessionRequest(prompt="hi"))
    await wait_turn(m1, resuming["session_id"])
    unstarted = await m1.create(CreateSessionRequest())  # no transcript: cannot resume
    await m1.shutdown()
    for sid in (resuming["session_id"], unstarted["session_id"]):
        meta = world.meta(sid)
        meta["state"] = "creating"  # a resume (or create) cut short by a service crash
        (world.session_dir(sid) / "session.json").write_text(json.dumps(meta))

    m2 = world.manager(world.cfg(sessions={"resume_on_start": True}))
    await m2.load_persisted()
    assert m2.get(resuming["session_id"])["state"] == "ready"
    assert m2.get(unstarted["session_id"])["state"] == "stopped"
    assert world.meta(unstarted["session_id"])["state"] == "stopped"
    argv = world.interactive_argvs()[-1]
    assert argv[argv.index("--resume") + 1] == resuming["claude_session_id"]
    await m2.shutdown()


async def test_open_event_log_and_turns_for_stopped_session(world: World) -> None:
    m = world.manager(world.cfg())
    snap = await m.create(CreateSessionRequest(prompt="hi"))
    sid = snap["session_id"]
    await wait_turn(m, sid)
    router = m.router(sid)
    assert m.open_event_log(sid) is router.event_log
    assert m.open_turns(sid) is router.turns
    await m.destroy(sid)

    log = m.open_event_log(sid)
    assert isinstance(log, EventLog) and log.readonly
    events, has_more = log.page(0, 1000)
    types = [e.type for e in events]
    assert "turn_started" in types and "turn_completed" in types and not has_more
    assert types[-1] in ("session_closed", "hook_result")
    turns = m.open_turns(sid)
    assert isinstance(turns, TurnStore) and turns.readonly
    [turn] = turns.list()
    assert (turn.origin, turn.state) == ("initial", "completed")

    # A fresh manager reads the same history from disk.
    m2 = world.manager(world.cfg())
    assert [e.seq for e in m2.open_event_log(sid).page(0, 1000)[0]] == [e.seq for e in events]
    assert m2.open_turns(sid).list()[0].turn_id == turn.turn_id
    for fn in (m.open_event_log, m.open_turns):
        with pytest.raises(SessionNotFound):
            fn("ses_ffffffffffff")


async def test_transcript_path_live_and_stopped(world: World) -> None:
    m = world.manager(world.cfg())
    snap = await m.create(CreateSessionRequest(prompt="hi"))
    sid = snap["session_id"]
    await wait_turn(m, sid)
    live = m.transcript_path(sid)
    assert live == m.router(sid).session.transcript_path
    await m.destroy(sid)
    assert m.transcript_path(sid) == live
    with pytest.raises(SessionNotFound):
        m.transcript_path("ses_ffffffffffff")


async def test_snapshot_state_vocabulary(world: World, tmp_path: Path) -> None:
    gate = tmp_path / "gate"
    cfg = world.cfg(
        init={"setup_commands": [f"while [ ! -f '{gate}' ]; do sleep 0.05; done"]},
    )
    m = world.manager(cfg)
    task = asyncio.ensure_future(m.create(CreateSessionRequest()))
    await wait_until(lambda: m.list(state="creating"))
    [creating] = m.list(state="creating")
    assert creating["state"] == "creating"
    assert m.get(creating["session_id"])["state"] == "creating"
    with pytest.raises(InvalidState):
        await m.destroy(creating["session_id"])
    gate.write_text("go")
    ready = await task
    assert ready["state"] == "ready"
    assert [s["session_id"] for s in m.list(state="ready")] == [ready["session_id"]]
    await m.destroy(ready["session_id"])

    keys = {
        "session_id",
        "claude_session_id",
        "name",
        "state",
        "workdir",
        "workdir_mode",
        "model",
        "effort",
        "created_at",
        "last_activity_at",
        "turns_completed",
        "current_turn_id",
        "stall_suspected_since",
        "last_fatal",
        "restarts",
        "env",
        "callbackUrl",
    }
    for snap in m.list():
        assert snap["state"] in PUBLIC_STATES
        assert set(snap) >= keys
    assert set(PUBLIC_STATES) == {"creating", "ready", "busy", "stopped", "failed", "dead"}
    for state in PUBLIC_STATES:
        assert all(s["state"] == state for s in m.list(state=state))
    with pytest.raises(ValueError):
        m.list(state="booting")


async def test_session_callback_url_default(world: World) -> None:
    m = world.manager(world.cfg())
    url = world.url + "/cb"
    snap = await m.create(CreateSessionRequest.model_validate({"callbackUrl": url, "prompt": "hi"}))
    sid = snap["session_id"]
    assert snap["callbackUrl"] == url
    assert world.meta(sid)["callbackUrl"] == url
    turn = await wait_turn(m, sid)
    await wait_until(lambda: any(c["type"] == "turn_completed" for c in world.callbacks))
    body = next(c for c in world.callbacks if c["type"] == "turn_completed")
    assert body["data"]["record"]["turn_id"] == turn.turn_id
    assert body["session_id"] == sid

    # A turn's own callbackUrl wins; the persisted default survives resume.
    await m.destroy(sid)
    await m.resume(sid)
    assert m.get(sid)["callbackUrl"] == url
    world.callbacks.clear()
    record = await m.router(sid).submit_turn("again")
    await wait_until(lambda: any(c["type"] == "turn_completed" for c in world.callbacks))
    assert world.callbacks[-1]["data"]["record"]["turn_id"] == record.turn_id
    with pytest.raises(ValueError):
        CreateSessionRequest.model_validate({"callback_url": url})


# ------------------------------------------------------------------ reaper and shutdown


async def test_idle_reaper_destroys_idle(world: World, tmp_path: Path) -> None:
    out = tmp_path / "on_destroy.jsonl"
    cfg = world.cfg(sessions={"idle_timeout_seconds": 0.6}, hooks={"onDestroy": [record_hook(out)]})
    m = world.manager(cfg, reaper_interval=0.1)
    snap = await m.create(CreateSessionRequest())
    sid = snap["session_id"]
    await wait_until(lambda: m.get(sid)["state"] == "stopped", 10)
    await wait_until(lambda: jsonl(out))
    assert world.session_dir(sid).is_dir()  # reaped without purge
    assert jsonl(out)[0]["event"] == "onDestroy"

    # idle_timeout_seconds 0 disables the reaper.
    m2 = world.manager(world.cfg(), reaper_interval=0.05)
    s2 = await m2.create(CreateSessionRequest())
    await asyncio.sleep(0.8)
    assert m2.get(s2["session_id"])["state"] == "ready"
    started = time.monotonic()
    await m2.shutdown()
    assert time.monotonic() - started < 2


def group_alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def test_shutdown_stops_all(world: World, tmp_path: Path) -> None:
    # The worst case: every claude keeps a TERM/HUP-ignoring child in its group (so only
    # SIGKILL ends it), onDestroy has a blocking action that sleeps, one session has a slow
    # onTurnComplete in flight, one is busy, one is mid-destroy with its caller gone, and
    # one is still creating.
    stubborn = tmp_path / "stubborn-claude"
    stubborn.write_text(
        "#!/bin/sh\n"
        "if [ -t 0 ]; then ( trap '' TERM HUP; exec sleep 60 ) </dev/null >/dev/null 2>&1 & fi\n"
        f"exec '{world.bin}' \"$@\"\n"
    )
    stubborn.chmod(0o755)
    out = tmp_path / "on_destroy"
    out.mkdir()
    record_each = {"type": "command", "run": f"cat > '{out}/'$$.json"}  # concurrent-safe
    gate = tmp_path / "gate"
    gate.write_text("open")
    slow_hook = {"type": "command", "run": "cat > /dev/null; sleep 30", "blocking": True}
    cfg = world.cfg(
        {"turns": [{"match": "slow", "steps": [{"sleep": 30}]}]},
        claude={"binary": str(stubborn)},
        hooks={
            "onDestroy": [record_each, {**slow_hook, "timeout_seconds": 30}],
            "onTurnComplete": [{**slow_hook, "timeout_seconds": 30}],  # holds the router
        },
        init={"setup_commands": [f"[ -f '{gate}' ] || sleep 30"]},
        sessions={"max_sessions": 6},
    )
    budget = cfg.limits.shutdown_timeout_seconds
    m = world.manager(cfg)
    snaps = [await m.create(CreateSessionRequest()) for _ in range(4)]
    ids = [s["session_id"] for s in snaps]
    pids = [m.router(sid).session.snapshot()["pid"] for sid in ids]
    assert all(pid_alive(p) for p in pids)
    await m.router(ids[0]).submit_turn("quick")
    await wait_turn(m, ids[0])
    await m.router(ids[1]).submit_turn("slow")
    await wait_until(lambda: m.get(ids[1])["state"] == "busy")
    closing = asyncio.ensure_future(m.destroy(ids[2]))  # waits out the full grace
    await asyncio.sleep(0.3)
    closing.cancel()  # the DELETE caller went away; the destroy itself carries on
    gate.unlink()
    stuck = asyncio.ensure_future(m.create(CreateSessionRequest()))
    await wait_until(lambda: m.list(state="creating"))
    [creating] = m.list(state="creating")
    started = time.monotonic()
    await m.shutdown()
    assert time.monotonic() - started < budget + 0.25  # the bound, plus scheduling slack
    assert not m.active_ids()
    assert not any(group_alive(p) for p in pids)  # the stubborn children are gone too
    with pytest.raises(asyncio.CancelledError):
        await closing
    with pytest.raises(asyncio.CancelledError):
        await stuck
    cancelled = m.get(creating["session_id"])
    assert cancelled["state"] == "failed"
    assert cancelled["error"] == {"step": "run_setup_commands", "message": "cancelled"}
    for sid in ids:
        assert m.get(sid)["state"] == "stopped"
        assert world.meta(sid)["state"] == "stopped"
    fired = [json.loads(f.read_text()) for f in out.iterdir()]
    assert sorted(p["session_id"] for p in fired) == sorted(ids)
    with pytest.raises(InvalidState):
        await m.create(CreateSessionRequest())


async def test_world_until_names_what_it_waited_for(world: World) -> None:
    await world.until(lambda: True)
    with pytest.raises(AssertionError, match="never-true-marker"):
        await world.until(lambda: False, timeout=0.2, what="never-true-marker")


async def test_world_wait_turn_returns_completed_turn(world: World) -> None:
    m = world.manager(world.cfg())
    snap = await m.create(CreateSessionRequest(prompt="hi"))
    turn = await world.wait_turn(m, snap["session_id"])
    assert turn.state == "completed"


# ------------------------------------------------------------------ slash command config


async def test_commands_override_stored_and_reapplied_on_resume(world: World) -> None:
    m = world.manager(world.cfg(sessions={"commands": {"agent": ["compact"], "allow_force": True}}))
    snap = await m.create(
        CreateSessionRequest(prompt="hi", commands={"client": ["context"], "allow_force": False})
    )
    sid = snap["session_id"]
    await wait_turn(m, sid)
    cmds = m.router(sid).commands_cfg
    assert cmds.agent == ["compact"] and cmds.client == ["context"] and cmds.allow_force is False
    assert world.meta(sid)["commands"] == {"client": ["context"], "allow_force": False}
    await m.destroy(sid)
    await m.resume(sid)
    cmds = m.router(sid).commands_cfg
    assert cmds.agent == ["compact"] and cmds.client == ["context"] and cmds.allow_force is False


async def test_no_commands_override_uses_config(world: World) -> None:
    m = world.manager(world.cfg(sessions={"commands": {"agent": ["context"]}}))
    snap = await m.create(CreateSessionRequest())
    assert m.router(snap["session_id"]).commands_cfg.agent == ["context"]
