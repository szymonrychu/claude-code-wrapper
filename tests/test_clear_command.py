"""`/clear` from API clients (0.10.0): a client-only command whose new claude session id the
wrapper follows (Stop gate, tailer, relaunch argv, persisted meta and resume)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import World, jsonl
from test_api_commands import TOKEN, H, settled
from test_bootstrap_settings import RELAY
from test_command_runner import commands_cfg, finished, of_type
from test_internal_api import FakeManager
from test_internal_api import H as INTERNAL_H
from test_internal_api import client as internal_client
from test_manager import record_hook

from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.bootstrap.settings import render_settings
from claude_code_wrapper.commands import CommandQueue, CommandRejected, effective_commands
from claude_code_wrapper.config import (
    CLIENT_ONLY_COMMANDS,
    DEFAULT_COMMAND_DENY,
    CommandsConfig,
    CommandsOverride,
    Config,
)
from claude_code_wrapper.hook_relay import CLAUDE_HOOK_EVENTS
from claude_code_wrapper.manager import CreateSessionRequest, SessionManager

CLIENT = ["clear", "compact"]
EMPTY = {"stdout": "", "stderr": "", "exit_code": 0}


def code_of(q: CommandQueue, name: str, origin: Any = "client") -> str | None:
    try:
        q.request(name, "", origin=origin, turn_id=None)
    except CommandRejected as exc:
        return exc.code
    return None


async def start(
    world: World,
    clear: dict | None = None,
    turns: list | None = None,
    hooks: dict | None = None,
    sessions: dict | None = None,
    **commands: Any,
) -> tuple[SessionManager, str, Any]:
    scenario: dict[str, Any] = {"commands": {"clear": clear or {}}, "turns": turns or []}
    cfg = world.cfg(
        scenario,
        server={"auth_token": TOKEN},
        hooks=hooks or {},
        sessions={"commands": commands_cfg(**{"client": CLIENT, **commands}), **(sessions or {})},
    )
    m = world.manager(cfg)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    return m, sid, m.router(sid)


def api(m: SessionManager) -> httpx.AsyncClient:
    app = create_app(m, m.cfg)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H)


async def clear(world: World, router: Any) -> Any:
    rec = router.request_command("clear", origin="client")
    await finished(world, rec)
    await world.until(lambda: router.session.state == "ready")
    return rec


def argv_flag(argv: list[str]) -> tuple[str, str]:
    for flag in ("--resume", "--session-id"):
        if flag in argv:
            return flag, argv[argv.index(flag) + 1]
    raise AssertionError(argv)


def transcript_of(world: World, sid: str, claude_sid: str) -> Path:
    projects = world.session_dir(sid) / "config" / "projects"
    return next(projects.glob("*")) / f"{claude_sid}.jsonl"


# ---------------------------------------------------------------- commands and config


def test_clear_left_the_deny_list_and_is_client_only():
    assert "clear" not in DEFAULT_COMMAND_DENY
    assert CLIENT_ONLY_COMMANDS == ("clear",)


async def test_clear_client_explicit_allowed(world):
    m, sid, router = await start(world)
    async with api(m) as c:
        r = await c.post(f"/v1/sessions/{sid}/commands", json={"name": "clear"})
        assert r.status_code == 202, r.text
        assert r.json()["name"] == "clear" and r.json()["origin"] == "client"
        final = await settled(c, f"/v1/sessions/{sid}/commands/{r.json()['id']}")
        assert (final["state"], final["result"]) == ("completed", "ok")


async def test_clear_client_wildcard_not_allowed(world):
    m, sid, router = await start(world, client=["*"])
    async with api(m) as c:
        r = await c.post(f"/v1/sessions/{sid}/commands", json={"name": "clear"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "command_not_allowed"
        assert "'*' does not admit it" in r.json()["error"]["message"]
    assert router.commands.list() == []
    assert code_of(CommandQueue(CommandsConfig(client=["*"])), "clear") == "not_allowed"
    # listed literally: the name as listed runs, another spelling does not
    assert code_of(CommandQueue(CommandsConfig(client=["clear"])), "Clear") == "not_allowed"


def test_clear_agent_denied():
    for agent in (["*"], ["clear"], ["clear", "*"]):
        q = CommandQueue(CommandsConfig(agent=agent, client=["clear"]))
        for name in ("clear", "CLEAR"):
            assert code_of(q, name, "agent") == "denied", (agent, name)
    assert code_of(CommandQueue(CommandsConfig(client=["clear"])), "clear", "idle") == (
        "not_allowed"
    )


async def test_clear_agent_denied_through_router(world):
    m, sid, router = await start(world, agent=["*", "clear"])
    with pytest.raises(CommandRejected) as exc:
        router.request_command("clear", origin="agent")
    assert exc.value.code == "denied"
    assert router.commands.list() == []


def test_reset_and_new_still_denied():
    q = CommandQueue(CommandsConfig(agent=["*"], client=["*", "reset", "new", "clear"]))
    for name in ("reset", "new", "Reset", "NEW"):
        assert name.lower() in DEFAULT_COMMAND_DENY
        for origin in ("agent", "client"):
            assert code_of(q, name, origin) == "denied", (name, origin)


def test_clear_override_deny_can_still_deny():
    base = CommandsConfig(client=["clear"])
    assert code_of(CommandQueue(effective_commands(base, None)), "clear") is None
    for deny in (["clear"], ["CLEAR"]):
        out = effective_commands(base, CommandsOverride(deny=deny))
        assert code_of(CommandQueue(out), "clear") == "denied", deny
    req = CreateSessionRequest.model_validate({"commands": {"deny": ["clear"]}})
    assert code_of(CommandQueue(effective_commands(base, req.commands)), "clear") == "denied"


async def test_clear_refused_with_outstanding_subagents(world):
    m, sid, router = await start(world)
    await router.handle_claude_hook(
        "SubagentStart", {"session_id": "x", "agent_id": "a1", "agent_type": "general-purpose"}
    )
    assert router.outstanding_subagents == 1
    async with api(m) as c:
        url = f"/v1/sessions/{sid}/commands"
        r = await c.post(url, json={"name": "clear"})
        assert r.status_code == 409 and r.json()["error"]["code"] == "subagents_running"
        assert router.commands.list() == []
        # a refused origin is refused first: the agent is denied, not told to wait
        with pytest.raises(CommandRejected) as exc:
            router.request_command("clear", origin="agent")
        assert exc.value.code == "denied"
        await router.handle_claude_hook(
            "SubagentStop", {"session_id": "x", "agent_id": "a1", "agent_type": "general-purpose"}
        )
        r = await c.post(url, json={"name": "clear"})
        assert r.status_code == 202, r.text
        final = await settled(c, f"{url}/{r.json()['id']}")
        assert final["state"] == "completed"


async def test_clear_rechecks_subagents_before_typing(world):
    turns = [{"match": "slow", "steps": [{"sleep": 1.5}, {"text": "slow done"}]}]
    m, sid, router = await start(world, turns=turns)
    turn = await router.submit_turn("slow work")
    rec = router.request_command("clear", origin="client")
    assert rec.state == "queued"
    await router.handle_claude_hook(
        "SubagentStart", {"session_id": "x", "agent_id": "a1", "agent_type": "general-purpose"}
    )
    await finished(world, rec)
    assert (rec.state, rec.error) == ("failed", "subagents_running")
    assert m.open_turns(sid).get(turn.turn_id).state == "completed"
    typed = [r["text"] for r in jsonl(world.session_dir(sid) / "config" / "fake-inputs.jsonl")]
    assert "/clear" not in typed


# ---------------------------------------------------------------- settings and relay


def test_session_start_relay_registered_with_clear_matcher(tmp_path):
    assert "SessionStart" in CLAUDE_HOOK_EVENTS
    doc = render_settings(tmp_path, Config(), relay_command=RELAY)
    (group,) = doc["hooks"]["SessionStart"]
    assert group["matcher"] == "clear"
    (hook,) = group["hooks"]
    assert hook["command"] == f"{RELAY} SessionStart"
    for event in CLAUDE_HOOK_EVENTS:
        if event != "SessionStart":
            assert doc["hooks"][event][0]["matcher"] == "", event


async def test_internal_api_accepts_session_start():
    mgr = FakeManager()
    payload = {"session_id": "new", "source": "clear", "hook_event_name": "SessionStart"}
    async with internal_client(mgr) as c:
        r = await c.post("/internal/hooks/SessionStart", json=payload, headers=INTERNAL_H)
    assert r.status_code == 200
    assert mgr.r.calls == [("SessionStart", payload)]


async def test_session_start_answer_is_empty(world):
    """Even while adopting, the relay answer injects nothing into the fresh conversation."""
    m, sid, router = await start(world, {"session_start_hook": False})
    payload = {"session_id": "11111111-2222-3333-4444-555555555555", "source": "clear"}
    assert await router.handle_claude_hook("SessionStart", payload) == EMPTY  # nothing running
    rec = router.request_command("clear", origin="client")
    await world.until(lambda: rec.state == "running")
    assert await router.handle_claude_hook("SessionStart", payload) == EMPTY
    await finished(world, rec)
    assert rec.state == "completed"
    assert router.session.spec.claude_session_id == payload["session_id"]


# ---------------------------------------------------------------- router


async def test_clear_adopts_new_session_id(world):
    m, sid, router = await start(world, {"hook_delay_seconds": 1.0})
    session = router.session
    old_id = session.spec.claude_session_id
    old_path = session.transcript_path
    await router.submit_turn("hello")
    await world.until(
        lambda: router.session.state == "ready" and router.session.current_turn is None
    )
    router.on_statusline(
        {"current_usage": {"input_tokens": 1000}, "context_window_size": 200000, "model": "m"}
    )
    assert router.context is not None
    router._last_marker = "turn-stale"
    rec = router.request_command("clear", origin="client")
    await world.until(lambda: rec.state == "running")
    # A subagent started while the SessionStart was still deferred belongs to the old chat.
    await router.handle_claude_hook(
        "SubagentStart", {"session_id": old_id, "agent_id": "a1", "agent_type": "general-purpose"}
    )
    assert router.outstanding_subagents == 1
    await finished(world, rec)
    assert (rec.state, rec.result, rec.error) == ("completed", "ok", None)
    new_id = session.spec.claude_session_id
    assert new_id != old_id
    assert router.info.claude_session_id == new_id
    assert session.transcript_path.endswith(f"/{new_id}.jsonl")
    assert session.transcript_path != old_path
    assert router.tailer.path == Path(session.transcript_path)
    assert router.subagents.path == Path(session.transcript_path)
    assert router.outstanding_subagents == 0 and router._link_waits == {}
    assert router._last_marker is None
    assert router.context is None
    (cleared,) = of_type(world, sid, "session_cleared")
    assert cleared == {
        "previous_claude_session_id": old_id,
        "claude_session_id": new_id,
        "transcript_path": session.transcript_path,
    }
    order = ["command_started", "context", "session_cleared", "command_completed"]
    seen = [e["type"] for e in world.events(sid) if e["type"] in order]
    assert seen[-4:] == order
    assert of_type(world, sid, "context")[-1]["used_tokens"] is None


async def test_session_start_clear_without_clear_command_ignored(world):
    m, sid, router = await start(world)
    session = router.session
    old_id, old_path = session.spec.claude_session_id, session.transcript_path
    tailer_path = router.tailer.path
    other = str(Path(old_path).with_name("other.jsonl"))
    payload = {"session_id": "other", "source": "clear", "transcript_path": other}
    assert await router.handle_claude_hook("SessionStart", payload) == EMPTY
    assert session.spec.claude_session_id == old_id and session.transcript_path == old_path
    assert router.info.claude_session_id == old_id
    assert router.tailer.path == tailer_path
    assert of_type(world, sid, "session_cleared") == []
    assert world.meta(sid)["claude_session_id"] == old_id


async def test_session_start_startup_ignored(world):
    m, sid, router = await start(
        world, {"session_start_hook": False}, timeout_seconds={"default": 2}
    )
    session = router.session
    old_id, tailer_path = session.spec.claude_session_id, router.tailer.path
    rec = router.request_command("clear", origin="client")
    await world.until(lambda: rec.state == "running")
    for source in ("startup", "resume", "compact", None):
        payload = {"session_id": f"id-{source}", "source": source, "transcript_path": "/x.jsonl"}
        assert await router.handle_claude_hook("SessionStart", payload) == EMPTY
    for bad in ("", None, 5):
        assert (
            await router.handle_claude_hook("SessionStart", {"session_id": bad, "source": "clear"})
            == EMPTY
        )
    await finished(world, rec)
    assert (rec.state, rec.error) == ("failed", "timeout")
    assert session.spec.claude_session_id == old_id and router.tailer.path == tailer_path
    assert of_type(world, sid, "session_cleared") == []


async def test_clear_timeout_without_session_start(world):
    m, sid, router = await start(
        world, {"session_start_hook": False}, timeout_seconds={"default": 2}
    )
    old_id = router.session.spec.claude_session_id
    rec = await clear(world, router)
    assert (rec.state, rec.error) == ("failed", "timeout")
    assert router.session.spec.claude_session_id == old_id
    assert router.info.claude_session_id == old_id
    assert world.meta(sid)["claude_session_id"] == old_id
    assert "previous_claude_session_ids" not in world.meta(sid)


async def test_clear_unknown_command_fails(world):
    m, sid, router = await start(world, {"unknown": True})
    old_id = router.session.spec.claude_session_id
    rec = await clear(world, router)
    assert (rec.state, rec.error) == ("failed", "unknown_command")
    assert router.session.spec.claude_session_id == old_id


async def test_clear_same_id_completes_without_event(world):
    m, sid, router = await start(world, {"session_start_hook": False})
    old_id = router.session.spec.claude_session_id
    rec = router.request_command("clear", origin="client")
    await world.until(lambda: rec.state == "running")
    await router.handle_claude_hook("SessionStart", {"session_id": old_id, "source": "clear"})
    await finished(world, rec)
    assert rec.state == "completed"
    assert of_type(world, sid, "session_cleared") == []
    assert "previous_claude_session_ids" not in world.meta(sid)


# ---------------------------------------------------------------- end to end, fake claude


async def test_turn_after_clear_completes(world):
    """The hang regression: the next turn's Stop carries the new id and completes the turn."""
    m, sid, router = await start(world)
    first = await router.submit_turn("before clear")
    await world.wait_turn(m, sid)
    await clear(world, router)
    new_id = router.session.spec.claude_session_id
    turn = await router.submit_turn("after clear")
    await world.until(lambda: m.open_turns(sid).get(turn.turn_id).state == "completed")
    done = m.open_turns(sid).get(turn.turn_id)
    assert done.final_text.startswith("echo: after clear") and done.turn_id != first.turn_id
    assert transcript_of(world, sid, new_id).is_file()
    # the tailer reads the new transcript: the turn's text arrived as an event
    texts = [d["text"] for d in of_type(world, sid, "assistant_text")]
    assert sum(t.startswith("echo: after clear") for t in texts) == 1


async def test_stop_with_old_id_after_clear_ignored(world):
    turns = [{"match": "slow", "steps": [{"sleep": 1.5}, {"text": "slow done"}]}]
    m, sid, router = await start(world, turns=turns)
    old_id = router.session.spec.claude_session_id
    await clear(world, router)
    turn = await router.submit_turn("slow work")
    await world.until(lambda: router.session.state == "busy")
    stale = {"session_id": old_id, "last_assistant_message": "stale", "stop_hook_active": False}
    assert await router.handle_claude_hook("Stop", stale) == EMPTY
    assert m.open_turns(sid).get(turn.turn_id).state == "running"
    await world.until(lambda: m.open_turns(sid).get(turn.turn_id).state == "completed")
    assert m.open_turns(sid).get(turn.turn_id).final_text == "slow done"


async def test_clear_waits_for_running_turn_then_runs(world):
    turns = [{"match": "slow", "steps": [{"sleep": 1.0}, {"text": "slow done"}]}]
    m, sid, router = await start(world, turns=turns)
    turn = await router.submit_turn("slow work")
    rec = router.request_command("clear", origin="client")
    assert rec.state == "queued" and rec.turn_id == turn.turn_id
    await finished(world, rec)
    assert rec.state == "completed"
    order = ["turn_completed", "command_started", "session_cleared", "command_completed"]
    assert [e["type"] for e in world.events(sid) if e["type"] in order] == order
    typed = jsonl(world.session_dir(sid) / "config" / "fake-inputs.jsonl")
    assert [(i["text"], i["state"]) for i in typed if i["text"] == "/clear"] == [("/clear", "idle")]


# ---------------------------------------------------------------- manager: meta and resume


async def test_clear_persists_meta_ids(world, tmp_path):
    out = tmp_path / "hooks.jsonl"
    hooks = {"onCommandComplete": [record_hook(out)], "onDestroy": [record_hook(out)]}
    m, sid, router = await start(world, hooks=hooks)
    old_id = router.session.spec.claude_session_id
    await clear(world, router)
    new_id = router.session.spec.claude_session_id
    meta = world.meta(sid)  # written at once, not at teardown
    assert meta["claude_session_id"] == new_id
    assert meta["transcript_path"] == router.session.transcript_path
    assert meta["previous_claude_session_ids"] == [old_id]
    assert m.get(sid)["claude_session_id"] == new_id
    assert m.transcript_path(sid) == router.session.transcript_path
    await world.until(lambda: jsonl(out))
    assert jsonl(out)[0]["event"] == "onCommandComplete"
    assert jsonl(out)[0]["claude_session_id"] == new_id
    # the list is capped: the newest 16 previous ids are kept
    ctx = m._active[sid]
    for i in range(20):
        m._on_claude_session_change(ctx, f"id-{i + 1}", f"/t/{i + 1}.jsonl", f"id-{i}")
    assert world.meta(sid)["previous_claude_session_ids"] == [f"id-{i}" for i in range(4, 20)]
    assert world.meta(sid)["claude_session_id"] == "id-20"
    # manager envelopes read the id from meta
    await m.destroy(sid)
    await world.until(lambda: len(jsonl(out)) == 2)
    assert [(p["event"], p["claude_session_id"]) for p in jsonl(out)] == [
        ("onCommandComplete", new_id),
        ("onDestroy", "id-20"),
    ]


async def test_resume_after_clear_without_turn_uses_session_id_flag(world):
    m, sid, router = await start(world)
    await router.submit_turn("before clear")
    await world.wait_turn(m, sid)
    await clear(world, router)
    new_id = router.session.spec.claude_session_id
    await m.destroy(sid)
    assert not transcript_of(world, sid, new_id).exists()
    resumed = await m.resume(sid)
    assert resumed["state"] == "ready" and resumed["claude_session_id"] == new_id
    assert argv_flag(world.interactive_argvs()[-1]) == ("--session-id", new_id)
    router = m.router(sid)
    turn = await router.submit_turn("fresh start")
    await world.until(lambda: m.open_turns(sid).get(turn.turn_id).state == "completed")


async def test_resume_after_clear_with_turn_resumes_new_id(world):
    m, sid, router = await start(world)
    await clear(world, router)
    new_id = router.session.spec.claude_session_id
    turn = await router.submit_turn("after clear")
    await world.until(lambda: m.open_turns(sid).get(turn.turn_id).state == "completed")
    await m.destroy(sid)
    resumed = await m.resume(sid)
    assert resumed["claude_session_id"] == new_id
    assert argv_flag(world.interactive_argvs()[-1]) == ("--resume", new_id)


async def test_crash_relaunch_after_clear_uses_new_id(world):
    turns = [{"match": "crash", "steps": [{"text": "about to crash"}, {"exit_once": 3}]}]
    m, sid, router = await start(world, turns=turns)
    await clear(world, router)
    new_id = router.session.spec.claude_session_id
    launches = len(world.interactive_argvs())
    await router.submit_turn("crash now")
    await world.until(lambda: len(world.interactive_argvs()) > launches)
    assert argv_flag(world.interactive_argvs()[-1]) == ("--resume", new_id)
    await world.until(lambda: router.session.state in ("ready", "busy"))
    assert router.session.spec.claude_session_id == new_id


async def test_resume_on_start_after_clear(world):
    """Pod restart: the killed process left meta live; the new process resumes the new id."""
    cfg_sessions = {"resume_on_start": True}
    m1, sid, router = await start(world, sessions=cfg_sessions)
    await router.submit_turn("before clear")
    await world.wait_turn(m1, sid)
    await clear(world, router)
    new_id = router.session.spec.claude_session_id
    await m1.shutdown()
    meta = world.meta(sid)
    meta["state"] = "ready"  # as a SIGKILL leaves it
    (world.session_dir(sid) / "session.json").write_text(json.dumps(meta))
    m2 = world.manager(m1.cfg)
    await m2.load_persisted()
    await world.until(lambda: m2.get(sid)["state"] == "ready")
    assert m2.get(sid)["claude_session_id"] == new_id
    assert argv_flag(world.interactive_argvs()[-1]) == ("--session-id", new_id)
