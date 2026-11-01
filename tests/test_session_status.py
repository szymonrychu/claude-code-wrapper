"""Session status for the UI: `waiting` (session-status-ui) and bootstrap step progress."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import World
from test_lanes import DIALOG, _lane_app
from test_session_turns import Harness, harness, wait_until  # noqa: F401 - fixture

from claude_code_wrapper import manager as manager_mod
from claude_code_wrapper.manager import CreateFailed, CreateSessionRequest

# ------------------------------------------------------------------ waiting


async def test_permission_prompt_sets_waiting_and_emits_events(world: World) -> None:
    m, sid, c = await _lane_app(world, DIALOG)
    session = m.router(sid).session
    assert m.get(sid)["waiting"] is None
    async with c:
        await c.post(f"/v1/sessions/{sid}/turns", json={"text": "edit it", "lane": "idle"})
        await world.until(session.dialog_open, what="the permission prompt on screen")
        waiting = m.get(sid)["waiting"]
        assert waiting["reason"] == "permission" and waiting["since"]
        started = [e for e in world.events(sid) if e["type"] == "waiting_started"]
        assert [e["data"]["reason"] for e in started] == ["permission"]
        assert started[0]["data"]["since"] == waiting["since"]
        session.note_hook()  # the tool ran: the prompt was answered
        assert m.get(sid)["waiting"] is None
        ended = [e for e in world.events(sid) if e["type"] == "waiting_ended"]
        assert [e["data"]["reason"] for e in ended] == ["permission"]


async def test_dialog_reason_when_no_turn_runs(harness: Harness) -> None:  # noqa: F811
    session = await harness.start({"turns": []})
    session._ui_dialog_at = session.clock()  # a footer seen while idle (a slash command dialog)
    session._sync_waiting()
    assert session.snapshot()["waiting"]["reason"] == "dialog"
    await session.send_escape()
    assert session.snapshot()["waiting"] is None


async def test_question_waits_until_next_prompt(harness: Harness) -> None:  # noqa: F811
    session = await harness.start({"turns": [{"match": "next", "steps": [{"text": "ok"}]}]})
    reasons: list[tuple[str | None, str | None]] = []
    original = harness.listener.__class__.on_waiting
    harness.listener.__class__.on_waiting = lambda self, reason, old: reasons.append((reason, old))
    try:
        session.set_question_waiting()
        assert session.snapshot()["waiting"]["reason"] == "question"
        await session.submit("next")
        assert session.snapshot()["waiting"] is None
    finally:
        harness.listener.__class__.on_waiting = original
    assert reasons == [("question", None), (None, "question")]


async def test_router_question_event_sets_waiting(world: World) -> None:
    m, sid, c = await _lane_app(world, {"turns": []})
    router = m.router(sid)
    async with c:
        await router.handle_claude_hook(
            "PreToolUse",
            {"tool_name": "AskUserQuestion", "tool_use_id": "t1", "tool_input": {"q": 1}},
        )
        assert m.get(sid)["waiting"]["reason"] == "question"
        types = [e["type"] for e in world.events(sid)]
        assert types.index("question") < types.index("waiting_started")


# ------------------------------------------------------------------ bootstrap steps


async def test_bootstrap_steps_in_snapshot_and_events(world: World, tmp_path: Path) -> None:
    m = world.manager(world.cfg())
    snap = await m.create(CreateSessionRequest())
    steps = snap["bootstrap"]["steps"]
    names = [s["step"] for s in steps]
    assert names[0] == "session_dir" and "install_plugins" in names and names[-1] == "publish_peer"
    assert all(s["status"] == "succeeded" and s["finished_at"] for s in steps)
    assert "ready" not in names
    ev = [e["data"] for e in world.events(snap["session_id"]) if e["type"] == "bootstrap_step"]
    # The event log opens inside session_dir, so that step's own "running" is not logged.
    assert (ev[0]["step"], ev[0]["status"]) == ("session_dir", "succeeded")
    assert [(e["step"], e["status"]) for e in ev[1:3]] == [
        ("prepare_workdir", "running"),
        ("prepare_workdir", "succeeded"),
    ]
    assert {e["status"] for e in ev} == {"running", "succeeded"}


async def test_failed_bootstrap_step_reports_short_safe_message(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = "sk-ant-abcdef0123456789"

    async def boom(*a: object, **kw: object) -> None:
        raise RuntimeError(f"superpowers not found in marketplace x {secret}\n" + "y" * 500)

    monkeypatch.setattr(manager_mod.SessionManager, "_install_plugins", boom)
    m = world.manager(world.cfg())
    with pytest.raises(CreateFailed):
        await m.create(CreateSessionRequest())
    sid = next((tmp_path / "sessions").iterdir()).name
    steps = world.meta(sid)["bootstrap"]["steps"]
    failed = [s for s in steps if s["status"] == "failed"]
    assert [s["step"] for s in failed] == ["install_plugins"]
    msg = failed[0]["message"]
    assert msg.startswith("superpowers not found in marketplace x ***")
    assert secret not in msg and "\n" not in msg and len(msg) <= 200
    ev = [e for e in world.events(sid) if e["type"] == "bootstrap_step"]
    assert ev[-1]["data"]["status"] == "failed" and ev[-1]["data"]["message"] == msg
    assert secret not in (world.session_dir(sid) / "events.jsonl").read_text()
