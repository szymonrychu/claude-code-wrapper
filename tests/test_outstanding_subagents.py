"""K10: the router counts subagents started and not yet stopped; the snapshot exposes it."""

from __future__ import annotations

from conftest import World, wait_turn
from test_router import Harness

from claude_code_wrapper.manager import CreateSessionRequest, SessionManager


async def _start(h, aid):
    await h.router.handle_claude_hook("SubagentStart", {"agent_id": aid, "agent_type": "x"})


async def _stop(h, aid):
    await h.router.handle_claude_hook("SubagentStop", {"agent_id": aid, "agent_type": "x"})


async def test_count_rises_and_falls_with_subagent_hooks(tmp_path):
    h = Harness(tmp_path)
    assert h.router.outstanding_subagents == 0
    await _start(h, "a1")
    await _start(h, "a2")
    await _start(h, "a1")  # duplicate start counts once
    assert h.router.outstanding_subagents == 2
    await _stop(h, "a1")
    assert h.router.outstanding_subagents == 1
    await h.router.close()


async def test_duplicate_stop_does_not_go_negative(tmp_path):
    h = Harness(tmp_path)
    await _start(h, "a1")
    await _stop(h, "a1")
    await _stop(h, "a1")
    await _stop(h, "unknown")
    assert h.router.outstanding_subagents == 0
    await h.router.close()


async def test_restart_clears_count(tmp_path):
    h = Harness(tmp_path)
    await _start(h, "a1")
    h.router.on_restart(1, True)
    assert h.router.outstanding_subagents == 0
    await _start(h, "a2")
    await h.router.close()
    assert h.router.outstanding_subagents == 0


async def test_stopped_session_reports_zero(world: World):
    m = world.manager(world.cfg())
    snap = await m.create(CreateSessionRequest(prompt="hi"))
    sid = snap["session_id"]
    await wait_turn(m, sid)
    await m.router(sid).handle_claude_hook("SubagentStart", {"agent_id": "a1", "agent_type": "x"})
    assert m.get(sid)["outstanding_subagents"] == 1
    assert [s for s in m.list() if s["session_id"] == sid][0]["outstanding_subagents"] == 1
    await m.destroy(sid)
    assert m.get(sid)["state"] == "stopped"
    assert m.get(sid)["outstanding_subagents"] == 0
    m2 = world.manager(world.cfg())
    await m2.load_persisted()
    assert m2.get(sid)["outstanding_subagents"] == 0


def test_compose_carries_the_count():
    meta = {"session_id": "ses_x"}
    kw = dict(state="ready", env={}, turns_completed=0, current=None, last_activity_at=None)
    assert SessionManager._compose(meta, live={}, **kw)["outstanding_subagents"] == 0
    assert (
        SessionManager._compose(meta, live={}, outstanding_subagents=3, **kw)[
            "outstanding_subagents"
        ]
        == 3
    )
