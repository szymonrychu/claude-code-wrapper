"""PermissionRequest relay: a held hook answered by a person, the deadline, the terminal or a
dropped connection, with `permission_requested` / `permission_resolved` (agent-dialogs W7).

End-to-end tests run the fake claude's permission prompt (its PermissionRequest hook goes
through the real `ccw-hook` to the router); the resolution-rule tests drive `handle_claude_hook`
directly on a live session so two pendings can coexist.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import World, jsonl
from test_api_turns import TOKEN
from test_command_runner import commands_cfg

from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.bootstrap.settings import permission_timeouts
from claude_code_wrapper.config import CommandsOverride
from claude_code_wrapper.hook_relay import PERMISSION_FAIL_MESSAGE, permission_output
from claude_code_wrapper.manager import CreateSessionRequest
from claude_code_wrapper.permissions import (
    GONE_DENY_MESSAGE,
    HOLD_DENY_MESSAGE,
    TERMINAL_DENY_MESSAGE,
)
from claude_code_wrapper.router import HOLD_GONE_WAITER, OPERATOR_DENY_MESSAGE

BASH = {"command": "echo probe"}


def scenario(*steps: dict[str, Any]) -> dict[str, Any]:
    return {"turns": [{"match": "go", "steps": list(steps)}]}


def gated(agent: dict | None = None) -> dict[str, Any]:
    step: dict[str, Any] = {"tool": "Bash", "input": BASH, "result": "probe", "permission": {}}
    if agent is not None:
        step["agent"] = agent
    return step


GATED = scenario(gated(), {"text": "after the tool"})


async def start(world: World, scen: dict, hold: float = 30.0, **sections: Any):
    cfg = world.cfg(scen, server={"auth_token": TOKEN}, **sections)
    m = world.manager(cfg)
    req = CreateSessionRequest(commands=CommandsOverride(permission_hold_seconds=hold))
    sid = (await m.create(req))["session_id"]
    return m, sid, m.router(sid)


def of_type(world: World, sid: str, type_: str) -> list[dict]:
    return [e["data"] for e in world.events(sid) if e["type"] == type_]


def tool_results(router: Any) -> list[dict]:
    """The tool_result blocks the fake wrote to the main transcript (what the model saw)."""
    out = []
    for line in jsonl(Path(router.session.transcript_path)):
        content = (line.get("message") or {}).get("content")
        if line.get("type") == "user" and isinstance(content, list):
            out += [c for c in content if c.get("type") == "tool_result"]
    return out


async def requested(world: World, sid: str) -> dict:
    await world.until(lambda: of_type(world, sid, "permission_requested"), 15)
    return of_type(world, sid, "permission_requested")[-1]


async def resolved(world: World, sid: str) -> dict:
    await world.until(lambda: of_type(world, sid, "permission_resolved"), 15)
    (only,) = of_type(world, sid, "permission_resolved")
    return only


def payload(agent_id: str | None = None, tool: str = "Bash", tinput: Any = None) -> dict:
    body: dict[str, Any] = {"tool_name": tool, "tool_input": BASH if tinput is None else tinput}
    if agent_id is not None:
        body["agent_id"] = agent_id
        body["agent_type"] = "general-purpose"
    return body


async def held(router: Any, body: dict, gone: asyncio.Event | None = None) -> asyncio.Task:
    """Start a PermissionRequest relay and wait until the broker holds it."""
    before = len(router.permissions)
    task = asyncio.ensure_future(router.handle_claude_hook("PermissionRequest", body, gone=gone))
    for _ in range(100):
        if len(router.permissions) > before:
            return task
        await asyncio.sleep(0.01)
    raise AssertionError("the relay never opened a pending permission")


def decision(response: dict) -> dict:
    return json.loads(response["stdout"])["hookSpecificOutput"]["decision"]


# ------------------------------------------------------------------ end to end (fake claude)


async def test_user_allow_runs_tool(world: World) -> None:
    m, sid, router = await start(world, GATED)
    await router.submit_turn("go")
    view = await requested(world, sid)
    assert view["tool_name"] == "Bash" and view["request_id"].startswith("perm_")
    assert router.session.waiting_reason() == "permission"
    assert router.answer_permission(view["request_id"], "allow", None) is True
    await world.wait_turn(m, sid)
    (result,) = tool_results(router)
    assert result["content"] == "probe" and result["is_error"] is False
    assert await resolved(world, sid) == {
        "request_id": view["request_id"],
        "by": "user",
        "behavior": "allow",
    }
    assert router.session.waiting_reason() is None
    assert len(router.permissions) == 0


async def test_user_deny_message_reaches_model(world: World) -> None:
    m, sid, router = await start(world, GATED)
    await router.submit_turn("go")
    view = await requested(world, sid)
    assert router.answer_permission(view["request_id"], "deny", "use the staging db") is True
    await world.wait_turn(m, sid)  # the Stop fired: a JSON deny does not interrupt the turn
    (result,) = tool_results(router)
    assert result["content"] == "use the staging db" and result["is_error"] is True
    got = await resolved(world, sid)
    assert (got["by"], got["behavior"]) == ("user", "deny")


async def test_user_deny_without_message_uses_operator_text(world: World) -> None:
    m, sid, router = await start(world, GATED)
    await router.submit_turn("go")
    view = await requested(world, sid)
    assert router.answer_permission(view["request_id"], "deny", None) is True
    await world.wait_turn(m, sid)
    (result,) = tool_results(router)
    assert result["content"] == OPERATOR_DENY_MESSAGE == "The operator denied this action"


async def test_hold_timeout_denies_with_spec_message(world: World) -> None:
    m, sid, router = await start(world, GATED, hold=0.5)
    await router.submit_turn("go")
    view = await requested(world, sid)
    await world.wait_turn(m, sid)
    (result,) = tool_results(router)
    assert result["content"] == HOLD_DENY_MESSAGE and result["is_error"] is True
    assert await resolved(world, sid) == {
        "request_id": view["request_id"],
        "by": "timeout",
        "behavior": "deny",
    }
    assert router.answer_permission(view["request_id"], "allow", None) is False
    assert router.session.waiting_reason() is None


async def test_terminal_answer_withdraws(world: World) -> None:
    m, sid, router = await start(world, GATED)
    await router.submit_turn("go")
    view = await requested(world, sid)
    await router.session.pty.write(b"1")  # the person at the TUI picks "1. Yes"
    await world.wait_turn(m, sid)
    (result,) = tool_results(router)
    assert result["content"] == "probe" and result["is_error"] is False
    assert await resolved(world, sid) == {
        "request_id": view["request_id"],
        "by": "terminal",
        "behavior": "allow",
    }
    assert len(router.permissions) == 0
    assert router.answer_permission(view["request_id"], "allow", None) is False
    (won,) = jsonl(world.session_dir(sid) / "config" / "fake-permissions.jsonl")
    assert (won["by"], won["behavior"]) == ("key", "allow")


async def test_pending_blocks_regular_lane(world: World) -> None:
    m, sid, router = await start(world, GATED)
    app = create_app(m, m.cfg)
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "go"})
        assert r.status_code == 202, r.text
        view = await requested(world, sid)
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "more", "lane": "regular"})
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "dialog_open"
        assert jsonl(world.session_dir(sid) / "config" / "fake-keys.jsonl") == []
        router.answer_permission(view["request_id"], "allow", None)
        await world.wait_turn(m, sid)


# ------------------------------------------------------------------ resolution rules


async def test_subagent_post_tool_use_does_not_withdraw_main(world: World) -> None:
    _, sid, router = await start(world, {"turns": []})
    main = await held(router, payload())
    sub_body = {**payload("a1"), "tool_use_id": "toolu_1", "tool_response": "probe"}
    await router.handle_claude_hook("PostToolUse", sub_body)
    await router.handle_claude_hook("SubagentStop", {"agent_id": "a1"})
    assert not main.done()
    assert len(router.permissions) == 1
    # The main agent's own PostToolUseFailure (same tool and input) withdraws it.
    main_body = {**payload(), "tool_use_id": "toolu_2", "error": "no"}
    await router.handle_claude_hook("PostToolUseFailure", main_body)
    out = await asyncio.wait_for(main, 2)
    assert decision(out)["behavior"] == "deny"
    got = await resolved(world, sid)
    assert (got["by"], got["behavior"]) == ("terminal", "deny")


async def test_subagent_stop_withdraws_only_that_agent(world: World) -> None:
    _, sid, router = await start(world, {"turns": []})
    main = await held(router, payload())
    sub = await held(router, payload("a1"))
    await router.handle_claude_hook("SubagentStop", {"agent_id": "a1"})
    assert decision(await asyncio.wait_for(sub, 2))["behavior"] == "deny"
    assert not main.done()
    await router.handle_claude_hook("Stop", {})
    assert decision(await asyncio.wait_for(main, 2))["behavior"] == "deny"
    assert {(e["by"], e["behavior"]) for e in of_type(world, sid, "permission_resolved")} == {
        ("terminal", "deny")
    }
    assert len(of_type(world, sid, "permission_resolved")) == 2


async def test_stop_leaves_subagent_pending(world: World) -> None:
    _, _, router = await start(world, {"turns": []})
    sub = await held(router, payload("a1"))
    await router.handle_claude_hook("Stop", {})
    assert not sub.done()
    await router.handle_claude_hook("SubagentStop", {"agent_id": "a1"})
    await asyncio.wait_for(sub, 2)


async def test_interrupted_turn_withdraws_main(world: World) -> None:
    """ "2. No" at the TUI interrupts the turn without PostToolUse or Stop: its end withdraws."""
    m, sid, router = await start(world, GATED)
    await router.submit_turn("go")
    view = await requested(world, sid)
    await router.session.pty.write(b"2")
    await world.wait_turn(m, sid, "interrupted")
    assert await resolved(world, sid) == {
        "request_id": view["request_id"],
        "by": "terminal",
        "behavior": "deny",
    }
    assert len(router.permissions) == 0
    assert router.session.waiting_reason() is None


async def test_close_resolves_pending_as_gone(world: World) -> None:
    _, sid, router = await start(world, {"turns": []})
    main = await held(router, payload())
    await router.close()
    out = await asyncio.wait_for(main, 1)
    assert decision(out) == {"behavior": "deny", "message": GONE_DENY_MESSAGE, "interrupt": False}
    got = of_type(world, sid, "permission_resolved")
    assert [(e["by"], e["behavior"]) for e in got] == [("gone", "deny")]


async def test_dropped_connection_resolves_as_gone(world: World) -> None:
    _, sid, router = await start(world, {"turns": []})
    gone = asyncio.Event()
    main = await held(router, payload(), gone=gone)
    gone.set()
    out = await asyncio.wait_for(main, 1)
    assert decision(out) == {"behavior": "deny", "message": GONE_DENY_MESSAGE, "interrupt": False}
    got = await resolved(world, sid)
    assert (got["by"], got["behavior"]) == ("gone", "deny")
    assert router.session.waiting_reason() is None


async def test_gone_leaves_drawn_prompt_visible(world: World, monkeypatch: Any) -> None:
    """The ccw-hook of a held PermissionRequest dies while the TUI prompt it raced is still
    drawn: the hook's arrival must not hide that prompt, so it shows as an open dialog."""
    m, sid, router = await start(world, GATED)
    gone = asyncio.Event()
    real = router.handle_claude_hook

    async def relay(event: str, body: Any, gone: Any = None) -> Any:
        if event != "PermissionRequest":
            return await real(event, body, gone)
        await real(event, body, gone=ev)
        return {"stdout": "", "exit_code": 0}  # the dead ccw-hook answers nothing

    ev = gone
    monkeypatch.setattr(router, "handle_claude_hook", relay)
    app = create_app(m, m.cfg)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t", headers=headers
    ) as c:
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "go"})
        assert r.status_code == 202, r.text
        await requested(world, sid)
        gone.set()
        got = await resolved(world, sid)
        assert (got["by"], got["behavior"]) == ("gone", "deny")
        session = router.session
        assert len(router.permissions) == 0
        assert session.dialog_open() is True
        assert session.dialog_kind() == "permission"
        assert session.waiting_reason() == "permission"
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "more", "lane": "regular"})
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "dialog_open"
        assert jsonl(world.session_dir(sid) / "config" / "fake-keys.jsonl") == []
        # Dismiss is the way out: ESC answers No on the still-drawn prompt.
        assert session.waiting_since is not None
        assert await session.escape_dialog(session.waiting_since) is True
        await world.wait_turn(m, sid, "interrupted")
    (won,) = jsonl(world.session_dir(sid) / "config" / "fake-permissions.jsonl")
    assert (won["by"], won["behavior"]) == ("key", "interrupt")


async def test_ask_user_question_passes_through(world: World) -> None:
    _, sid, router = await start(world, {"turns": []})
    body = payload(tool="AskUserQuestion", tinput={"questions": []})
    out = await router.handle_claude_hook("PermissionRequest", body)
    assert out["stdout"] == ""
    assert of_type(world, sid, "permission_requested") == []
    assert len(router.permissions) == 0


async def test_handler_error_fails_closed(world: World, monkeypatch: Any) -> None:
    _, _, router = await start(world, {"turns": []})

    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("broker down")

    monkeypatch.setattr(router.permissions, "open", boom)
    out = await router.handle_claude_hook("PermissionRequest", payload())
    assert out["stdout"] == permission_output("deny", PERMISSION_FAIL_MESSAGE)
    assert out["exit_code"] == 0


async def test_hold_deadline_cancelled_without_leaking_tasks(world: World) -> None:
    _, _, router = await start(world, {"turns": []})
    gone = asyncio.Event()
    main = await held(router, payload(), gone=gone)
    view = router.permissions.first()
    assert view is not None
    assert router.answer_permission(view.request_id, "allow", None) is True
    out = await asyncio.wait_for(main, 1)
    assert decision(out) == {"behavior": "allow"}
    await asyncio.sleep(0)
    leaked = [t for t in asyncio.all_tasks() if t.get_name() == HOLD_GONE_WAITER]
    assert leaked == []


def test_hold_deadline_strictly_below_hook_timeouts() -> None:
    """The relay answers first: its deadline is the hold, under both the ccw-hook HTTP timeout
    and the claude hook timeout, so a hold never falls back to the TUI prompt (probe case 4)."""
    for hold in (0.5, 30.0, 1800.0, 86400.0):
        t = permission_timeouts(hold)
        assert hold < t.relay_timeout < t.hook_timeout


# ------------------------------------------------------------------ no agent self-approval


@pytest.mark.parametrize("event", ["PostToolUse", "PostToolUseFailure", "Stop"])
async def test_forged_terminal_hook_never_allows(world: World, event: str) -> None:
    """CCW_HOOK_TOKEN is in every Bash tool's env, so an agent can post any hook: a terminal
    resolution answers the held hook with a fixed deny, never allow. A real keypress already
    answered claude first (probe case 3b), so claude ignores this later output anyway."""
    _, sid, router = await start(world, {"turns": []})
    main = await held(router, payload())
    body = {} if event == "Stop" else {**payload(), "tool_use_id": "toolu_x", "tool_response": "ok"}
    await router.handle_claude_hook(event, body)
    out = await asyncio.wait_for(main, 2)
    assert decision(out) == {
        "behavior": "deny",
        "message": TERMINAL_DENY_MESSAGE,
        "interrupt": False,
    }
    got = await resolved(world, sid)
    assert got["by"] == "terminal"


# ------------------------------------------------------------------ no CR over a held permission

IDLE = {"turns": [{"match": "^go", "steps": [{"text": "done"}]}]}
CONTINUE = "Resume after commands"
GO = {"match": "^go", "steps": [{"sleep": 0.8}, {"text": "part one"}]}
RESUMED = {"match": "^Resume after commands", "steps": [{"text": "part two"}]}


def typed(world: World, sid: str) -> list[str]:
    return [i["text"] for i in jsonl(world.session_dir(sid) / "config" / "fake-inputs.jsonl")]


async def held_subagent(world: World, scen: dict = IDLE, **sections: Any):
    """A ready session with a background subagent's permission held (the main Stop is done)."""
    m, sid, router = await start(world, scen, **sections)
    await world.until(lambda: router.session.state == "ready", 15)
    sub = await held(router, payload("a1"))
    return m, sid, router, sub


async def release(router: Any, sub: asyncio.Task) -> None:
    await router.handle_claude_hook("SubagentStop", {"agent_id": "a1"})
    await asyncio.wait_for(sub, 2)


async def test_idle_lane_refused_while_permission_held(world: World) -> None:
    m, sid, router, sub = await held_subagent(world)
    app = create_app(m, m.cfg)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "go", "lane": "idle"})
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "dialog_open"
        assert typed(world, sid) == []
        await release(router, sub)
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "go", "lane": "idle"})
        assert r.status_code == 202, r.text
        await world.wait_turn(m, sid)


async def test_probe_refused_while_permission_held(world: World) -> None:
    m, sid, router, sub = await held_subagent(world)
    app = create_app(m, m.cfg)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        r = await c.post(f"/v1/sessions/{sid}/probe")
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "dialog_open"
    assert typed(world, sid) == []
    await release(router, sub)


async def test_command_runner_waits_for_held_permission(world: World) -> None:
    m, sid, router, sub = await held_subagent(world, sessions={"commands": commands_cfg()})
    rec = router.request_command("compact", origin="client")
    await asyncio.sleep(1.0)
    assert rec.state == "queued"
    assert typed(world, sid) == []
    await release(router, sub)
    await world.until(lambda: rec.state in ("completed", "failed", "cancelled"), 15)
    assert (rec.state, rec.result) == ("completed", "ok")
    assert typed(world, sid) == ["/compact"]


async def test_continuation_waits_for_held_permission(world: World, monkeypatch: Any) -> None:
    cmds = commands_cfg(continue_prompt=CONTINUE, agent=["*"])
    m, sid, router = await start(world, {"turns": [GO, RESUMED]}, sessions={"commands": cmds})
    session = router.session
    real = session.wait_quiet
    subs: list[asyncio.Task] = []
    recs: list[Any] = []

    async def quiet(seconds: float, timeout: float) -> bool:
        # the command ran; a background subagent's prompt opens before the continuation
        if recs and recs[0].state == "completed" and not subs:
            subs.append(await held(router, payload("a1")))
        return await real(seconds, timeout)

    monkeypatch.setattr(session, "wait_quiet", quiet)
    turn = await router.submit_turn("go")
    recs.append(router.request_command("compact", origin="agent"))
    await world.until(lambda: subs, 15)
    await asyncio.sleep(1.0)
    assert turn.state == "running"
    assert [t for t in typed(world, sid) if t.startswith(CONTINUE)] == []
    await release(router, subs[0])
    done = await world.wait_turn(m, sid)
    assert done.turn_id == turn.turn_id and done.final_text == "part two"
    assert len([t for t in typed(world, sid) if t.startswith(CONTINUE)]) == 1
