"""Session permission waiting, dialog kind and the guarded dismiss ESC (agent-dialogs W6)."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import jsonl
from test_session_turns import SID, Harness, harness, wait_until  # noqa: F401 - fixture

from claude_code_wrapper.session import Session, TurnRejected

PERMISSION = "Do you want to proceed?\n 1. Yes\n 2. No\n Esc to cancel \u00b7 Tab to amend"
MODEL_DIALOG = "Select model\n 1. Default\n Esc to cancel"
USAGE = "Usage limit reached \u00b7 continuing automatically at 3pm \u00b7 esc to cancel"


def scenario(text: str) -> dict[str, Any]:
    return {"turns": [{"match": "go", "steps": [{"text": text}, {"sleep": 30}]}]}


def keys(h: Harness) -> list[dict]:
    return jsonl(h.tmp_path / "sessions" / SID / "config" / "fake-keys.jsonl")


async def open_dialog(h: Harness, text: str) -> Session:
    session = await h.start(scenario(text))
    await session.submit("go")
    await wait_until(session.dialog_open)
    await wait_until(lambda: session.waiting_since is not None)
    return session


async def test_permission_pending_sets_waiting_and_blocks_lanes(harness: Harness) -> None:  # noqa: F811
    session = await harness.start({"turns": []})
    assert session.waiting_reason() is None
    session.set_permission_pending(True)
    assert session.waiting_reason() == "permission"
    assert session.waiting_since is not None
    with pytest.raises(TurnRejected) as exc:
        session.check_no_dialog()
    assert exc.value.reason == "dialog_open"
    session.set_permission_pending(False)
    assert session.waiting_reason() is None
    assert session.waiting_since is None
    session.check_no_dialog()


async def test_question_outranks_permission_pending(harness: Harness) -> None:  # noqa: F811
    session = await harness.start({"turns": []})
    session.set_question_waiting()
    session.set_permission_pending(True)
    assert session.waiting_reason() == "question"


async def test_dialog_kind_none_without_dialog(harness: Harness) -> None:  # noqa: F811
    session = await harness.start({"turns": []})
    assert session.dialog_kind() is None
    assert not session.auto_escape_blocked()


async def test_dialog_kind_dialog(harness: Harness) -> None:  # noqa: F811
    session = await open_dialog(harness, MODEL_DIALOG)
    assert session.dialog_kind() == "dialog"
    assert not session.auto_escape_blocked()


async def test_usage_wait_kind(harness: Harness) -> None:  # noqa: F811
    session = await open_dialog(harness, USAGE)
    assert session.dialog_kind() == "usage_wait"
    assert session.auto_escape_blocked()


async def test_footer_only_permission_kind(harness: Harness) -> None:  # noqa: F811
    session = await open_dialog(harness, PERMISSION)
    assert session.dialog_kind() == "permission"
    assert not session.auto_escape_blocked()


async def test_permission_pending_wins_over_footer(harness: Harness) -> None:  # noqa: F811
    session = await open_dialog(harness, MODEL_DIALOG)
    session.set_permission_pending(True)
    assert session.dialog_kind() == "permission"
    assert session.auto_escape_blocked()
    assert session.waiting_reason() == "permission"
    since = session.waiting_since
    assert since is not None
    assert await session.escape_dialog(since) is False
    assert keys(harness) == []


async def test_escape_dialog_once_per_since(harness: Harness) -> None:  # noqa: F811
    session = await open_dialog(harness, MODEL_DIALOG)
    since = session.waiting_since
    assert since is not None
    assert await session.escape_dialog(since) is True
    assert await session.escape_dialog(since) is False
    await wait_until(lambda: len(keys(harness)) >= 1)
    assert len(keys(harness)) == 1
    assert not session.dialog_open()


async def test_escape_dialog_refuses_changed_since_and_usage_wait(harness: Harness) -> None:  # noqa: F811
    session = await open_dialog(harness, MODEL_DIALOG)
    assert await session.escape_dialog("not-the-since") is False
    assert keys(harness) == []
    await session.stop(grace=1.0)
    session = await open_dialog(harness, USAGE)
    since = session.waiting_since
    assert since is not None
    assert await session.escape_dialog(since) is False
    assert keys(harness) == []
