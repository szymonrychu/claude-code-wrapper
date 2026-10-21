"""DEF-2: a rejected turn fires no hooks and never touches workdir files."""

from __future__ import annotations

import asyncio

import pytest
from test_router import Harness, webhook
from test_session_turns import Harness as SessHarness

from claude_code_wrapper.session import BootError, TurnRejected


async def _rejected(tmp_path, reason, files):
    h = Harness(tmp_path, hooks={"onTurn": [webhook()]})
    try:
        (h.workdir / "a.txt").write_text("old")
        h.session.reject = reason
        with pytest.raises(TurnRejected) as exc:
            await h.router.submit_turn("hi", files=files)
        assert exc.value.reason == reason
        assert (h.workdir / "a.txt").read_text() == "old"
        assert not (h.workdir / "b.txt").exists()
        return list(h.hook_posts)
    finally:
        await h.router.close()


async def test_busy_submit_writes_no_files(tmp_path):
    await _rejected(tmp_path, "busy", {"a.txt": "new", "b.txt": "x"})


async def test_busy_submit_fires_no_on_turn(tmp_path):
    assert await _rejected(tmp_path, "busy", {"a.txt": "new"}) == []


@pytest.mark.parametrize("reason", ["booting", "dead"])
async def test_booting_and_dead_rejected_before_files(tmp_path, reason):
    posts = await _rejected(tmp_path, reason, {"a.txt": "new", "b.txt": "x"})
    assert posts == []


async def test_check_submittable_matches_submit_reasons(tmp_path, fake_claude_bin, scenario_file):
    """The real Session: same reasons and order as submit, and no side effects."""
    h = SessHarness(tmp_path, fake_claude_bin, scenario_file)
    try:
        booting = h.build()
        with pytest.raises(TurnRejected) as exc:
            booting.check_submittable()
        assert exc.value.reason == "booting"
        dead = h.build(binary=str(tmp_path / "no-such-claude"))
        with pytest.raises(BootError):
            await dead.start()
        with pytest.raises(TurnRejected) as exc:
            dead.check_submittable()
        assert exc.value.reason == "dead"

        session = await h.start({"turns": [{"match": "slow", "steps": [{"sleep": 30}]}]})
        assert session.check_submittable() is session.pty
        assert session.state == "ready" and session.current_turn is None  # no side effects
        record = await session.submit("slow one")
        for check in (session.check_submittable, lambda: session.submit("again")):
            with pytest.raises(TurnRejected) as exc:
                result = check()
                if asyncio.iscoroutine(result):
                    await result
            assert exc.value.reason == "busy"
        assert session.current_turn is record
    finally:
        for session in h.made:
            await session.stop(grace=1.0)


async def test_second_check_guards_the_write(tmp_path):
    """A slot taken while onTurn ran still rejects before any file is written."""
    h = Harness(tmp_path, hooks={"onTurn": [webhook()]})
    try:
        (h.workdir / "a.txt").write_text("old")
        orig = h.router._fire

        async def fire(event, data, **kw):
            out = await orig(event, data, **kw)
            h.session.reject = "busy"  # the slot is taken during the hook
            return out

        h.router._fire = fire
        with pytest.raises(TurnRejected):
            await h.router.submit_turn("hi", files={"a.txt": "new"})
        assert (h.workdir / "a.txt").read_text() == "old"
    finally:
        await h.router.close()
