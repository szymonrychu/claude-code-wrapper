"""DEF-2: a rejected turn fires no hooks and never touches workdir files."""

from __future__ import annotations

import pytest
from test_router import Harness, webhook

from claude_code_wrapper.session import TurnRejected


@pytest.mark.parametrize("reason", ["busy", "booting", "dead"])
async def test_rejected_turn_leaves_workdir_untouched(tmp_path, reason):
    h = Harness(tmp_path, hooks={"onTurn": [webhook()]})
    try:
        (h.workdir / "a.txt").write_text("old")
        h.session.reject = reason
        with pytest.raises(TurnRejected) as exc:
            await h.router.submit_turn("hi", files={"a.txt": "new", "b.txt": "x"})
        assert exc.value.reason == reason
        assert (h.workdir / "a.txt").read_text() == "old"
        assert not (h.workdir / "b.txt").exists()
        assert h.hook_posts == []
    finally:
        await h.router.close()


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
