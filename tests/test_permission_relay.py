"""PermissionRequest relay: a held hook answered by a person, the deadline, the terminal or a
dropped connection, with `permission_requested` / `permission_resolved` (agent-dialogs W7).

End-to-end tests run the fake claude's permission prompt (its PermissionRequest hook goes
through the real `ccw-hook` to the router); the resolution-rule tests drive `handle_claude_hook`
directly on a live session so two pendings can coexist.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import World, jsonl
from test_api_turns import TOKEN
from test_command_runner import commands_cfg

from claude_code_wrapper import session as session_mod
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


async def test_user_allow_runs_tool(world: World, monkeypatch: Any) -> None:
    # Fix round 5: a person's allow sends no ESC, but the writers stay locked until the footer
    # leaves the screen or the settle rule lifts it.
    settle_fast(monkeypatch)
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
    await world.until(lambda: router.session.waiting_reason() is None, 3, "lock lifted")
    assert len(router.permissions) == 0
    assert keys(world, sid) == []


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
    # Fix round 5: every non-allow resolution also gets one ESC, after the grace that lets a
    # live hook's deny reach claude first, and the writers stay locked meanwhile.
    await world.until(lambda: keys(world, sid) == ["esc"], 3, "deny ESC")
    assert router.session.permission_prompt_open() is True


async def test_user_deny_without_message_uses_operator_text(world: World) -> None:
    m, sid, router = await start(world, GATED)
    await router.submit_turn("go")
    view = await requested(world, sid)
    assert router.answer_permission(view["request_id"], "deny", None) is True
    await world.wait_turn(m, sid)
    (result,) = tool_results(router)
    assert result["content"] == OPERATOR_DENY_MESSAGE == "The operator denied this action"
    # Fix round 5: one ESC after the deny.
    await world.until(lambda: keys(world, sid) == ["esc"], 3, "deny ESC")


async def test_hold_timeout_denies_with_spec_message(world: World, monkeypatch: Any) -> None:
    # Fix round 5: the timeout also gets one ESC, and the waiting reason clears only when the
    # writer lock lifts (settle rule).
    settle_fast(monkeypatch)
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
    await world.until(lambda: keys(world, sid) == ["esc"], 3, "timeout ESC")
    await world.until(lambda: router.session.waiting_reason() is None, 3, "lock lifted")


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
        "behavior": "deny",
        "terminal_observed": "allow",
    }
    assert len(router.permissions) == 0
    assert router.answer_permission(view["request_id"], "allow", None) is False
    (won,) = jsonl(world.session_dir(sid) / "config" / "fake-permissions.jsonl")
    assert (won["by"], won["behavior"]) == ("key", "allow")
    # Fix round 5: a terminal resolution is forgeable, so it gets one ESC too.
    await world.until(lambda: keys(world, sid) == ["1", "esc"], 3, "terminal ESC")


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


async def test_interrupted_turn_withdraws_main(world: World, monkeypatch: Any) -> None:
    """ "2. No" at the TUI interrupts the turn without PostToolUse or Stop: its end withdraws.
    Fix round 5: the terminal resolution gets one ESC and the lock lifts on the settle rule."""
    settle_fast(monkeypatch)
    m, sid, router = await start(world, GATED)
    await router.submit_turn("go")
    view = await requested(world, sid)
    await router.session.pty.write(b"2")
    await world.wait_turn(m, sid, "interrupted")
    assert await resolved(world, sid) == {
        "request_id": view["request_id"],
        "by": "terminal",
        "behavior": "deny",
        "terminal_observed": "deny",
    }
    assert len(router.permissions) == 0
    await world.until(lambda: keys(world, sid) == ["2", "esc"], 3, "terminal ESC")
    await world.until(lambda: router.session.waiting_reason() is None, 3, "lock lifted")


async def test_close_resolves_pending_as_gone(world: World) -> None:
    _, sid, router = await start(world, {"turns": []})
    main = await held(router, payload())
    await router.close()
    out = await asyncio.wait_for(main, 1)
    assert decision(out) == {"behavior": "deny", "message": GONE_DENY_MESSAGE, "interrupt": False}
    got = of_type(world, sid, "permission_resolved")
    assert [(e["by"], e["behavior"]) for e in got] == [("gone", "deny")]


async def test_dropped_connection_resolves_as_gone(world: World, monkeypatch: Any) -> None:
    # Premise changed (fix round 4): the orphan gets its ESC with no prompt seen and the lock
    # lifts after ORPHAN_SETTLE_SECONDS with no footer after it, instead of a 5 s drop.
    monkeypatch.setattr(session_mod, "ORPHAN_SETTLE_SECONDS", 0.3)
    _, sid, router = await start(world, {"turns": []})
    gone = asyncio.Event()
    main = await held(router, payload(), gone=gone)
    gone.set()
    out = await asyncio.wait_for(main, 1)
    assert decision(out) == {"behavior": "deny", "message": GONE_DENY_MESSAGE, "interrupt": False}
    got = await resolved(world, sid)
    assert (got["by"], got["behavior"]) == ("gone", "deny")
    await world.until(lambda: keys(world, sid) == ["esc"], 3, "orphan ESC")
    await world.until(lambda: router.session.waiting_reason() is None, 3, "lock lifted")


def keys(world: World, sid: str) -> list[str]:
    return [k["key"] for k in jsonl(world.session_dir(sid) / "config" / "fake-keys.jsonl")]


def settle_fast(monkeypatch: Any) -> None:
    """Lift the orphan lock 0.3 s after the last orphan ESC when no footer follows it. Only for
    tests that wait for the lift; a test asserting the lock still holds keeps the 30 s default
    and ends the window itself with `settle_now`, so a slow runner cannot lift it early."""
    monkeypatch.setattr(session_mod, "ORPHAN_SETTLE_SECONDS", 0.3)


async def settle_now(session: Any, monkeypatch: Any) -> None:
    """End the settle window at once and wait for the writer task's decision: lift the lock, or
    keep it because a footer was seen after the last ESC. No real-time window to race."""
    monkeypatch.setattr(session_mod, "ORPHAN_SETTLE_SECONDS", 0.0)
    session._orphan_wake.set()
    await asyncio.wait_for(asyncio.shield(session._orphan_task), 5)


class EscGate:
    """Parks every ESC the session writes (`_write_escape`) until the test lets it through, so
    "not sent yet" and "still locked" checks do not race a grace or spacing window."""

    def __init__(self, session: Any, monkeypatch: Any) -> None:
        self.parked = 0
        self._tokens = asyncio.Semaphore(0)
        real = session._write_escape

        async def gated(pty: Any, guard: Any) -> bool:
            self.parked += 1
            await self._tokens.acquire()
            return await real(pty, guard)

        monkeypatch.setattr(session, "_write_escape", gated)

    def release(self, n: int = 1) -> None:
        for _ in range(n):
            self._tokens.release()


def esc_done(session: Any) -> bool:
    """Every ESC owed is written and recorded as the last one (the settle window's start)."""
    return session._orphans == 0 and session._orphan_esc_at == session.last_escape


async def test_dropped_hook_denies_fallback_prompt_once(world: World, monkeypatch: Any) -> None:
    """The ccw-hook of a held PermissionRequest dies (an agent can kill it): claude falls back
    to its TUI prompt with "1. Yes" highlighted (probe case 5). Nothing types a CR over it, the
    wrapper answers it with exactly one ESC (No), and the prompt closes."""
    settle_fast(monkeypatch)
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
        session = router.session
        gate = EscGate(session, monkeypatch)  # writers meet the orphaned prompt before its ESC
        gone.set()
        got = await resolved(world, sid)
        assert (got["by"], got["behavior"]) == ("gone", "deny")
        assert len(router.permissions) == 0
        assert session.permission_prompt_open() is True
        assert session.dialog_kind() == "permission"
        assert session.waiting_reason() == "permission"
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "more", "lane": "regular"})
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "dialog_open"
        r = await c.post(f"/v1/sessions/{sid}/probe")
        assert r.status_code == 409, r.text
        assert keys(world, sid) == []
        gate.release()
        await world.wait_turn(m, sid, "interrupted")
    assert keys(world, sid) == ["esc"]
    (won,) = jsonl(world.session_dir(sid) / "config" / "fake-permissions.jsonl")
    assert (won["by"], won["behavior"]) == ("key", "interrupt")
    assert [t for t in typed(world, sid) if not t.startswith("go")] == []
    await world.until(lambda: not session.permission_prompt_open(), 3, "lock lifted")
    assert session.dialog_open() is False


async def test_orphaned_prompt_blocks_writers_until_its_escape(
    world: World, monkeypatch: Any
) -> None:
    """A background subagent's hook is dropped at an idle session: the idle lane and the probe
    are refused and a queued command waits until the one ESC has closed the prompt."""
    settle_fast(monkeypatch)
    m, sid, router = await start(world, IDLE, sessions={"commands": commands_cfg()})
    await world.until(lambda: router.session.state == "ready", 15)
    gone = asyncio.Event()
    sub = await held(router, payload("a1"), gone=gone)
    draw_prompt(router.session)
    gate = EscGate(router.session, monkeypatch)
    gone.set()
    out = await asyncio.wait_for(sub, 2)
    assert decision(out)["behavior"] == "deny"
    app = create_app(m, m.cfg)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "go", "lane": "idle"})
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "dialog_open"
        r = await c.post(f"/v1/sessions/{sid}/probe")
        assert r.status_code == 409, r.text
    rec = router.request_command("compact", origin="client")
    await asyncio.sleep(0.5)
    assert rec.state == "queued"
    assert typed(world, sid) == [] and keys(world, sid) == []
    gate.release()
    await world.until(lambda: rec.state in ("completed", "failed", "cancelled"), 15)
    assert (rec.state, rec.result) == ("completed", "ok")
    assert keys(world, sid) == ["esc"]
    assert typed(world, sid) == ["/compact"]
    esc = jsonl(world.session_dir(sid) / "config" / "fake-keys.jsonl")[0]["ts"]
    line = jsonl(world.session_dir(sid) / "config" / "fake-inputs.jsonl")[0]["ts"]
    assert esc < line


def draw_prompt(session: Any) -> None:
    """The TUI permission prompt claude draws in parallel with the hook (probe case 1), for a
    background subagent the fake cannot prompt for at idle."""
    prompt = (
        "Do you want to proceed?\r\n> 1. Yes\r\n  2. No\r\nEsc to cancel \u00b7 Tab to amend\r\n"
    )
    session._on_output(session.pty, prompt.encode())


async def idle_orphans(world: World, *agents: str, **sections: Any):
    """A ready session whose background subagents' held hooks (`agents`) all just dropped."""
    m, sid, router = await start(world, IDLE, **sections)
    await world.until(lambda: router.session.state == "ready", 15)
    gone = asyncio.Event()
    subs = [await held(router, payload(a), gone=gone) for a in agents]
    gone.set()
    for sub in subs:
        assert decision(await asyncio.wait_for(sub, 2))["behavior"] == "deny"
    return m, sid, router


def esc_times(world: World, sid: str) -> list[datetime]:
    return [
        datetime.fromisoformat(k["ts"])
        for k in jsonl(world.session_dir(sid) / "config" / "fake-keys.jsonl")
    ]


class EscWrites:
    """Session-clock times of the ESCs the wrapper really wrote to the PTY. Spacing is measured
    here: the fake's own timestamps are when it read a key, which a loaded runner delays."""

    def __init__(self, session: Any, monkeypatch: Any) -> None:
        self.at: list[float] = []
        real = session.pty.write_guarded

        async def spy(data: bytes, guard: Any) -> bool:
            wrote = await real(data, guard)
            if wrote and data == session_mod.ESC:
                self.at.append(session.clock())
            return wrote

        monkeypatch.setattr(session.pty, "write_guarded", spy)

    def assert_spaced(self) -> None:
        for one, two in zip(self.at, self.at[1:], strict=False):
            assert two - one >= session_mod.ESC_SPACING_SECONDS


async def test_orphans_dropped_together_get_one_escape_each(world: World, monkeypatch: Any) -> None:
    """Two background subagents' hooks are killed at once (one gone event): claude shows its
    queued prompts one at a time. Each orphan gets its own spaced ESC, the writers stay locked
    until both are sent, and a hook in between does not lift the lock. Premise changed (fix
    round 4): the second ESC no longer waits for its prompt to be drawn."""
    m, sid, router = await start(world, IDLE, sessions={"commands": commands_cfg()})
    session = router.session
    await world.until(lambda: session.state == "ready", 15)
    escs = EscWrites(session, monkeypatch)
    gone = asyncio.Event()
    subs = [await held(router, payload(a), gone=gone) for a in ("a1", "a2")]
    draw_prompt(session)
    gone.set()
    for sub in subs:
        assert decision(await asyncio.wait_for(sub, 2))["behavior"] == "deny"
    await world.until(lambda: len(escs.at) >= 1, 5, "first ESC")
    # A hook from another agent (or a forged one) arrives between the two ESCs.
    await router.handle_claude_hook("PostToolUse", {**payload(), "tool_response": "ok"})
    assert session.permission_prompt_open() is True
    with pytest.raises(session_mod.TurnRejected):
        session.check_no_permission()
    app = create_app(m, m.cfg)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "go", "lane": "idle"})
        assert r.status_code == 409, r.text
        r = await c.post(f"/v1/sessions/{sid}/probe")
        assert r.status_code == 409, r.text
    await world.until(lambda: keys(world, sid) == ["esc", "esc"], 5, "second ESC")
    escs.assert_spaced()
    assert typed(world, sid) == []
    await world.until(lambda: esc_done(session), 5, "ESCs recorded")
    await settle_now(session, monkeypatch)
    assert not session.permission_prompt_open()
    await asyncio.sleep(1.2)
    assert keys(world, sid) == ["esc", "esc"]  # exactly one ESC per orphan
    assert typed(world, sid) == []


async def test_orphan_whose_prompt_never_shows_still_gets_one_escape(
    world: World, monkeypatch: Any
) -> None:
    """No footer is ever seen (the scanner missed it, or the prompt is not drawn yet): the
    orphan still gets exactly one ESC, and the writers stay locked until it is sent."""
    settle_fast(monkeypatch)
    m, sid, router = await start(world, IDLE)
    session = router.session
    await world.until(lambda: session.state == "ready", 15)
    gate = EscGate(session, monkeypatch)
    gone = asyncio.Event()
    sub = await held(router, payload("a1"), gone=gone)
    gone.set()
    await asyncio.wait_for(sub, 2)
    await world.until(lambda: gate.parked == 1, 5, "orphan ESC due")
    assert keys(world, sid) == []
    assert session.permission_prompt_open() is True
    gate.release()
    await world.until(lambda: keys(world, sid) == ["esc"], 5, "orphan ESC")
    await world.until(lambda: not session.permission_prompt_open(), 3, "lock lifted")
    await asyncio.sleep(1.6)
    assert keys(world, sid) == ["esc"]
    assert typed(world, sid) == []


async def test_typed_footer_echo_neither_skips_escape_nor_unlocks(
    world: World, monkeypatch: Any
) -> None:
    """The last typed text quotes the permission footer, so the echo filter hides the real
    footer from the dialog scanner. The orphan still gets its ESC, and a footer after it keeps
    the lock (the scanner cannot tell an echo from a prompt), until a person dismisses it.
    The settle window stays at its 30 s default until the footer is drawn and `settle_now`
    ends it: a slow runner can no longer lift the lock before the footer lands (CI flake)."""
    m, sid, router = await start(world, IDLE)
    session = router.session
    await world.until(lambda: session.state == "ready", 15)
    escs = EscWrites(session, monkeypatch)
    await router.submit_turn("go: press Esc to cancel \u00b7 Tab to amend")
    await world.wait_turn(m, sid)
    gone = asyncio.Event()
    sub = await held(router, payload("a1"), gone=gone)
    draw_prompt(session)
    gone.set()
    await asyncio.wait_for(sub, 2)
    assert session.permission_prompt_open() is True
    await world.until(lambda: keys(world, sid) == ["esc"], 5, "orphan ESC")
    await world.until(lambda: esc_done(session), 5, "orphan ESC recorded")
    draw_prompt(session)  # a prompt (or an echo) drawn after the last orphan ESC
    await settle_now(session, monkeypatch)  # the settle window ended with the footer seen
    assert session._orphan_lock is True
    assert session.permission_prompt_open() is True
    with pytest.raises(session_mod.TurnRejected):
        session.check_no_permission()
    assert keys(world, sid) == ["esc"]
    assert len(typed(world, sid)) == 1
    # The human dismiss path clears a stuck lock with its own (spaced) ESC.
    out = await router.answer_dialog("dismiss", None, None, None)
    assert out == {"status": "dismissed", "escape_sent": True}
    assert session.permission_prompt_open() is False
    await world.until(lambda: keys(world, sid) == ["esc", "esc"], 5, "dismiss ESC")
    assert len(escs.at) == 2
    escs.assert_spaced()


async def test_late_prompt_is_escaped_and_stays_locked(world: World) -> None:
    """A queued prompt drawn more than 5 s after its orphaning (a loaded pod): the orphan's
    ESC is already sent, the footer after it keeps the writers locked."""
    m, sid, router = await idle_orphans(world, "a1")
    session = router.session
    await world.until(lambda: keys(world, sid) == ["esc"], 3, "orphan ESC")
    await asyncio.sleep(5.2)
    assert session.permission_prompt_open() is True
    draw_prompt(session)
    await asyncio.sleep(0.2)
    assert session.permission_prompt_open() is True
    assert session.dialog_kind() == "permission"
    assert keys(world, sid) == ["esc"]
    assert typed(world, sid) == []


async def test_forged_hooks_do_not_lift_orphan_lock(world: World) -> None:
    """Every hook event is forgeable (CCW_HOOK_TOKEN is in each Bash tool's env): none of them
    lifts the orphan lock, before or after its ESC."""
    m, sid, router = await idle_orphans(world, "a1")
    session = router.session
    forged = [
        ("PostToolUse", {**payload("a1"), "tool_response": "ok"}),
        ("SubagentStop", {"agent_id": "a1"}),
        ("Stop", {}),
    ]
    for event, body in forged:
        await router.handle_claude_hook(event, body)
    assert session.permission_prompt_open() is True
    await world.until(lambda: keys(world, sid) == ["esc"], 3, "orphan ESC")
    for event, body in forged:
        await router.handle_claude_hook(event, body)
    assert session.permission_prompt_open() is True
    with pytest.raises(session_mod.TurnRejected):
        session.check_no_permission()


async def test_relaunch_clears_orphan_lock(world: World) -> None:
    """A relaunched claude shows no prompt of the old one: the lock clears at once."""
    m, sid, router = await idle_orphans(world, "a1")
    session = router.session
    await world.until(lambda: keys(world, sid) == ["esc"], 3, "orphan ESC")
    draw_prompt(session)  # stuck: a footer after the last orphan ESC
    assert session.permission_prompt_open() is True
    assert await session.restart_for_dialog() is True
    assert session.permission_prompt_open() is False
    assert session.dialog_open() is False


async def test_interrupt_escape_keeps_spacing_after_orphan_escape(
    world: World, monkeypatch: Any
) -> None:
    """An interrupt ESC queued on the PTY lock behind an orphan ESC is re-spaced under the
    lock, so the two never land back to back (a double ESC opens rewind)."""
    slow = {"turns": [{"match": "^go", "steps": [{"sleep": 5}, {"text": "done"}]}]}
    m, sid, router = await start(world, slow)
    session = router.session
    await world.until(lambda: session.state == "ready", 15)
    await router.submit_turn("go")
    await world.until(lambda: session.state == "busy", 5)
    escs = EscWrites(session, monkeypatch)
    gone = asyncio.Event()
    sub = await held(router, payload("a1"), gone=gone)
    async with session.pty._lock:  # both ESC writers queue on the lock
        gone.set()
        await asyncio.wait_for(sub, 2)
        await asyncio.sleep(0.05)
        interrupt = asyncio.ensure_future(router.interrupt())
        await asyncio.sleep(0.05)
    await asyncio.wait_for(interrupt, 10)
    await world.until(lambda: len(escs.at) >= 2, 5, "two ESCs")
    escs.assert_spaced()


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
    # The event reports what the hook really answered, not what the forged hook claimed.
    assert got["behavior"] == "deny"
    assert got["terminal_observed"] == ("allow" if event == "PostToolUse" else "deny")


async def test_restart_resolves_held_as_gone(world: World) -> None:
    """A crash relaunch answers every held hook at once (its claude is gone)."""
    _, sid, router = await start(world, {"turns": []})
    main = await held(router, payload())
    sub = await held(router, payload("a1"))
    router.on_restart(1, True)
    assert not router.session.dialog_open()  # at once, before the held handlers run
    for task in (main, sub):
        out = await asyncio.wait_for(task, 1)
        assert decision(out) == {
            "behavior": "deny",
            "message": GONE_DENY_MESSAGE,
            "interrupt": False,
        }
    got = of_type(world, sid, "permission_resolved")
    assert [(e["by"], e["behavior"]) for e in got] == [("gone", "deny"), ("gone", "deny")]
    # The relaunched claude shows no prompt of the old one: nothing is left to deny.
    assert router.session.permission_prompt_open() is False
    await asyncio.sleep(1.2)
    assert keys(world, sid) == []


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


async def test_idle_lane_refused_while_permission_held(world: World, monkeypatch: Any) -> None:
    # Fix round 5: the release (terminal) gets one ESC; the lane opens once the lock lifts.
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
        assert r.status_code == 409, r.text  # locked until the ESC and the settle
        await world.until(lambda: keys(world, sid) == ["esc"], 5, "release ESC")
        await world.until(lambda: esc_done(router.session), 5, "release ESC recorded")
        await settle_now(router.session, monkeypatch)
        assert not router.session.permission_prompt_open()
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


async def test_command_runner_waits_for_held_permission(world: World, monkeypatch: Any) -> None:
    # Fix round 5: the release gets one ESC, then the lock settles before the command types.
    settle_fast(monkeypatch)
    m, sid, router, sub = await held_subagent(world, sessions={"commands": commands_cfg()})
    rec = router.request_command("compact", origin="client")
    await asyncio.sleep(1.0)
    assert rec.state == "queued"
    assert typed(world, sid) == []
    await release(router, sub)
    await world.until(lambda: rec.state in ("completed", "failed", "cancelled"), 15)
    assert (rec.state, rec.result) == ("completed", "ok")
    assert typed(world, sid) == ["/compact"]
    assert keys(world, sid) == ["esc"]


async def test_continuation_waits_for_held_permission(world: World, monkeypatch: Any) -> None:
    # Fix round 5: the release gets one ESC and a settled lock before the continuation.
    settle_fast(monkeypatch)
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


# ------------------------------------------------------------------ stopped hook (fix round 5)


def show(session: Any, text: str) -> None:
    """Output claude writes, through the ring as the PTY reader does."""
    session.pty.ring.append(text.encode())
    session._on_output(session.pty, text.encode())


async def stopped_hook(world: World, hold: float = 30.0):
    """A ready session holding a main-agent relay whose ccw-hook is stopped (SIGSTOP): its
    connection stays open, so no `gone`, and whatever answer the relay writes never reaches
    claude. The TUI prompt drawn in parallel stays live with "1. Yes" highlighted."""
    m, sid, router = await start(world, IDLE, hold=hold)
    await world.until(lambda: router.session.state == "ready", 15)
    task = await held(router, payload(), gone=asyncio.Event())
    draw_prompt(router.session)
    return m, sid, router, task


async def assert_escaped_and_locked(world: World, sid: str, session: Any) -> None:
    assert session.permission_prompt_open() is True
    with pytest.raises(session_mod.TurnRejected):
        session.check_no_permission()
    await world.until(lambda: keys(world, sid) == ["esc"], 3, "one ESC")
    await asyncio.sleep(1.2)
    assert keys(world, sid) == ["esc"]
    assert session.permission_prompt_open() is True
    assert typed(world, sid) == []


async def test_stopped_hook_forged_terminal_escapes_and_locks(world: World) -> None:
    """S1: the agent stops its hook and forges a PostToolUse. The terminal resolution denies,
    but the hook never prints it: the wrapper sends one ESC and the writers stay locked."""
    m, sid, router, task = await stopped_hook(world)
    body = {**payload(), "tool_use_id": "toolu_x", "tool_response": "ok"}
    await router.handle_claude_hook("PostToolUse", body)
    assert decision(await asyncio.wait_for(task, 2))["behavior"] == "deny"
    await assert_escaped_and_locked(world, sid, router.session)


async def test_stopped_hook_user_deny_escapes_and_locks(world: World) -> None:
    m, sid, router, task = await stopped_hook(world)
    view = router.permissions.first()
    assert view is not None
    assert router.answer_permission(view.request_id, "deny", None) is True
    assert decision(await asyncio.wait_for(task, 2))["behavior"] == "deny"
    await assert_escaped_and_locked(world, sid, router.session)


async def test_stopped_hook_timeout_escapes_and_locks(world: World) -> None:
    m, sid, router, task = await stopped_hook(world, hold=0.5)
    assert decision(await asyncio.wait_for(task, 3))["behavior"] == "deny"
    assert (await resolved(world, sid))["by"] == "timeout"
    await assert_escaped_and_locked(world, sid, router.session)


async def test_user_allow_sends_no_escape_and_locks_until_footer_gone(world: World) -> None:
    """A person's allow is the one answer the live "1. Yes" may carry out: no ESC. The writers
    stay locked until the footer is gone from the screen (or the settle rule)."""
    m, sid, router, task = await stopped_hook(world)
    session = router.session
    show(session, "Do you want to proceed?\r\nEsc to cancel \u00b7 Tab to amend\r\n")
    view = router.permissions.first()
    assert view is not None
    assert router.answer_permission(view.request_id, "allow", None) is True
    assert decision(await asyncio.wait_for(task, 2)) == {"behavior": "allow"}
    assert session.permission_prompt_open() is True
    show(session, "Running the tool\r\n")  # the footer is still in the screen tail
    assert session.permission_prompt_open() is True
    await asyncio.sleep(1.2)
    assert keys(world, sid) == []
    assert session.permission_prompt_open() is True
    show(session, "tool output " * (session_mod.DIALOG_TAIL_CHARS // 10))
    assert session.permission_prompt_open() is False
    await asyncio.sleep(0.2)
    assert keys(world, sid) == []


async def test_user_allow_lock_lifts_on_settle(world: World, monkeypatch: Any) -> None:
    """No output after a person's allow: the lock lifts on the settle rule, with no ESC."""
    settle_fast(monkeypatch)
    m, sid, router, task = await stopped_hook(world)
    view = router.permissions.first()
    assert view is not None
    router.answer_permission(view.request_id, "allow", None)
    await asyncio.wait_for(task, 2)
    assert router.session.permission_prompt_open() is True
    await world.until(lambda: not router.session.permission_prompt_open(), 3, "lock lifted")
    await asyncio.sleep(1.2)
    assert keys(world, sid) == []


# ------------------------------------------------------------------ grace and mixed order


async def test_deny_escape_waits_out_grace_with_lock_held(world: World, monkeypatch: Any) -> None:
    """A user deny locks the writers at once, but its ESC only lands after the grace that lets
    a live hook's deny reach claude first. The ESC is parked at a gate, so the "not sent yet,
    still locked" checks hold however slow the runner is."""
    m, sid, router, task = await stopped_hook(world)
    session = router.session
    gate = EscGate(session, monkeypatch)
    view = router.permissions.first()
    assert view is not None
    start_at = datetime.now(UTC)
    resolved_at = session.clock()
    assert router.answer_permission(view.request_id, "deny", None) is True
    assert decision(await asyncio.wait_for(task, 2))["behavior"] == "deny"
    assert session.permission_prompt_open() is True
    with pytest.raises(session_mod.TurnRejected):
        session.check_no_permission()
    await world.until(lambda: gate.parked == 1, 5, "deny ESC due")
    # the writer only tries its ESC once the grace is over
    assert session.clock() - resolved_at >= session_mod.PERMISSION_ESC_GRACE_SECONDS
    assert keys(world, sid) == []
    assert session.permission_prompt_open() is True
    assert typed(world, sid) == []
    gate.release()
    await world.until(lambda: keys(world, sid) == ["esc"], 5, "deny ESC")
    (esc,) = esc_times(world, sid)
    assert (esc - start_at).total_seconds() >= session_mod.PERMISSION_ESC_GRACE_SECONDS - 0.15
    assert session.permission_prompt_open() is True


async def mixed_pair(world: World, monkeypatch: Any, first: str, second: str) -> None:
    """Two held relays resolved in sequence, each by `deny` (a person) or `gone` (a dropped
    hook): one spaced ESC each, writers locked until both are sent. The settle window keeps
    its 30 s default until `settle_now`, so the lock checks cannot race it."""
    m, sid, router = await start(world, IDLE)
    session = router.session
    await world.until(lambda: session.state == "ready", 15)
    escs = EscWrites(session, monkeypatch)
    gones = {"a1": asyncio.Event(), "a2": asyncio.Event()}
    tasks = {a: await held(router, payload(a), gone=gones[a]) for a in ("a1", "a2")}
    draw_prompt(session)
    for agent, how in (("a1", first), ("a2", second)):
        if how == "deny":
            (view,) = router.permissions.of_agent(agent)
            assert router.answer_permission(view.request_id, "deny", None) is True
        else:
            gones[agent].set()
        assert decision(await asyncio.wait_for(tasks[agent], 2))["behavior"] == "deny"
        assert session.permission_prompt_open() is True
    await world.until(lambda: len(escs.at) >= 1, 5, "first ESC")
    assert session.permission_prompt_open() is True
    with pytest.raises(session_mod.TurnRejected):
        session.check_no_permission()
    await world.until(lambda: keys(world, sid) == ["esc", "esc"], 5, "second ESC")
    assert len(escs.at) == 2
    escs.assert_spaced()
    assert typed(world, sid) == []
    assert session.permission_prompt_open() is True
    await world.until(lambda: esc_done(session), 5, "ESCs recorded")
    await settle_now(session, monkeypatch)
    assert not session.permission_prompt_open()
    await asyncio.sleep(1.2)
    assert keys(world, sid) == ["esc", "esc"]  # exactly one ESC per resolution


async def test_deny_then_orphan_get_spaced_escapes(world: World, monkeypatch: Any) -> None:
    await mixed_pair(world, monkeypatch, "deny", "gone")


async def test_orphan_then_deny_get_spaced_escapes(world: World, monkeypatch: Any) -> None:
    await mixed_pair(world, monkeypatch, "gone", "deny")
