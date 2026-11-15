"""Context pressure at Stop: inventory on every Stop, bounded block, resume check."""

from __future__ import annotations

import json
from pathlib import Path

from test_pressure_injection import policy, status
from test_router import CLAUDE_SID, SID, Harness, webhook

from claude_code_wrapper.pressure import PressureState
from claude_code_wrapper.timeutil import now_iso
from claude_code_wrapper.turns import TurnRecord

L2 = 142_000
L3 = 170_000


def make(tmp_path: Path, used: int | None = L2, hooks: dict | None = None, **pol) -> Harness:
    h = Harness(tmp_path, hooks=hooks)
    h.cfg.pressure.enabled = True
    h.state = PressureState(h.cfg.pressure)
    h.state.put(policy())
    h.router.pressure = h.state
    if used is not None:
        status(h, used)
    if pol:
        h.state.put(policy().model_copy(update={"version": 2, **pol}))
    record = new_turn(h, "turn-1")
    h.record = record
    return h


def new_turn(h: Harness, turn_id: str) -> TurnRecord:
    record = TurnRecord(turn_id=turn_id, session_id=SID, state="running", started_at=now_iso())
    h.session.turn = record
    h.session.stop_target = lambda payload, marker=None: record  # type: ignore[attr-defined]
    return record


async def stop(h: Harness, **payload):
    return await h.router.handle_claude_hook("Stop", {"session_id": CLAUDE_SID, **payload})


def blocked(r: dict) -> str | None:
    if not r["stdout"]:
        return None
    out = json.loads(r["stdout"])
    return out["reason"] if out.get("decision") == "block" else None


async def test_stop_records_inventory_every_stop(tmp_path):
    h = make(tmp_path, used=None)
    assert h.cfg.sessions.defer_stop_on_background_tasks is False
    tasks = [{"id": "b1", "command": "sleep 9"}]
    crons = [{"id": "c1", "cron": "* * * * *"}]
    await stop(h, background_tasks=tasks, session_crons=crons)
    inv = h.router.stop_inventory
    assert inv["background_tasks"] == tasks and inv["session_crons"] == crons and inv["at"]
    await stop(h)
    assert h.router.stop_inventory["background_tasks"] == []
    await h.router.close()


async def test_l2_blocks_once_per_turn(tmp_path):
    h = make(tmp_path)
    r = await stop(h)
    assert "FULL2 71%" in blocked(r)
    (ev,) = h.events("pressure_stop_blocked")
    assert ev.data == {"turn_id": "turn-1", "level": 2, "count": 1, "reason": "pressure"}
    assert h.events("turn_continued")[0].data["reason"] == blocked(r)
    assert blocked(await stop(h, stop_hook_active=True)) is None
    await h.router.close()


async def test_l3_blocks_three_times_then_allows(tmp_path):
    h = make(tmp_path, used=L3)
    for n in (1, 2, 3):
        assert "FULL3" in blocked(await stop(h, stop_hook_active=n > 1))
    assert blocked(await stop(h, stop_hook_active=True)) is None
    assert [e.data["count"] for e in h.events("pressure_stop_blocked")] == [1, 2, 3]
    await h.router.close()


async def test_no_block_when_submitted_with_background_tasks(tmp_path):
    h = make(tmp_path, used=L3, handoff_state="submitted")
    r = await stop(h, background_tasks=[{"id": "b1"}])
    assert blocked(r) is None and not h.events("pressure_stop_blocked")
    await h.router.close()


async def test_no_block_when_accepted(tmp_path):
    h = make(tmp_path, used=L3, handoff_state="accepted")
    assert blocked(await stop(h)) is None
    await h.router.close()


async def test_blocked_state_still_blocks(tmp_path):
    h = make(tmp_path, used=L3, handoff_state="blocked")
    assert blocked(await stop(h)) is not None
    await h.router.close()


async def test_stop_hook_active_at_bound_allows(tmp_path):
    h = make(tmp_path)
    assert blocked(await stop(h)) is not None
    assert blocked(await stop(h, stop_hook_active=True)) is None
    await h.router.close()


async def test_counter_resets_next_turn(tmp_path):
    h = make(tmp_path)
    assert blocked(await stop(h)) is not None
    assert blocked(await stop(h, stop_hook_active=True)) is None
    new_turn(h, "turn-2")
    assert blocked(await stop(h)) is not None
    assert [e.data["turn_id"] for e in h.events("pressure_stop_blocked")] == ["turn-1", "turn-2"]
    await h.router.close()


async def test_no_block_below_l2_or_without_policy_or_off_target(tmp_path):
    h = make(tmp_path, used=110_000)
    assert blocked(await stop(h)) is None
    status(h, L2)
    h.session.turn = None  # the Stop does not target the running turn
    assert blocked(await stop(h)) is None
    h.state.clear()
    new_turn(h, "turn-3")
    assert blocked(await stop(h)) is None
    await h.router.close()


async def test_resume_check_blocks_once_without_tool_call(tmp_path):
    h = make(tmp_path, used=None, resume_pending=True, resume_text="RESUME now")
    assert blocked(await stop(h)) == "RESUME now"
    (ev,) = h.events("pressure_stop_blocked")
    assert ev.data["reason"] == "resume"
    assert blocked(await stop(h, stop_hook_active=True)) is None
    assert len(h.events("pressure_stop_blocked")) == 1
    await h.router.close()


async def test_resume_check_cleared_by_tool_call(tmp_path):
    h = make(tmp_path, used=None, resume_pending=True, resume_text="RESUME now")
    h.record.tool_calls = 1
    assert blocked(await stop(h)) is None
    new_turn(h, "turn-2")
    assert blocked(await stop(h)) is None  # the flag is gone
    await h.router.close()


async def test_block_reason_has_live_pct_no_time_words(tmp_path):
    h = make(tmp_path, used=L3)
    reason = blocked(await stop(h))
    assert "85%" in reason
    for word in ("minute", "deadline", "seconds"):
        assert word not in reason.lower()
    await h.router.close()


async def test_onstop_hook_still_runs_when_pressure_allows(tmp_path):
    h = make(tmp_path, used=110_000, hooks={"onStop": [webhook()]})
    assert blocked(await stop(h)) is None
    assert [p["event"] for p in h.hook_posts] == ["onStop"]
    await h.router.close()
