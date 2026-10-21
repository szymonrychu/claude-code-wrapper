"""K10: the router counts subagents started and not yet stopped; the snapshot exposes it."""

from __future__ import annotations

from test_router import Harness

from claude_code_wrapper.manager import SessionManager


async def _start(h, aid):
    await h.router.handle_claude_hook("SubagentStart", {"agent_id": aid, "agent_type": "x"})


async def _stop(h, aid):
    await h.router.handle_claude_hook("SubagentStop", {"agent_id": aid, "agent_type": "x"})


async def test_count_tracks_start_and_stop(tmp_path):
    h = Harness(tmp_path)
    assert h.router.outstanding_subagents == 0
    await _start(h, "a1")
    await _start(h, "a2")
    await _start(h, "a1")  # duplicate start counts once
    assert h.router.outstanding_subagents == 2
    await _stop(h, "a1")
    assert h.router.outstanding_subagents == 1
    await _stop(h, "unknown")  # ignored
    assert h.router.outstanding_subagents == 1
    await h.router.close()


async def test_cleared_on_restart_and_close(tmp_path):
    h = Harness(tmp_path)
    await _start(h, "a1")
    h.router.on_restart(1, True)
    assert h.router.outstanding_subagents == 0
    await _start(h, "a2")
    await h.router.close()
    assert h.router.outstanding_subagents == 0


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
