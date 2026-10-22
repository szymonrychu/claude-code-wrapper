"""Context usage from the statusLine relay: snapshot field and the `context` event."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claude_code_wrapper.events import EventLog
from claude_code_wrapper.internal_api import parse_statusline
from claude_code_wrapper.manager import SessionManager
from claude_code_wrapper.redact import Redactor
from claude_code_wrapper.router import SessionRouter


def make_router(tmp_path: Path) -> SessionRouter:
    router = SessionRouter.__new__(SessionRouter)
    router._event_log = EventLog(tmp_path / "events.jsonl", "ses_1")
    router._redactor = Redactor()
    router._idle_start = None
    router._idle_only = True
    router._last_real = 0.0
    router.statusline = None
    router.context = None
    return router


def payload(
    *,
    pct: float | None = 12.5,
    size: int | None = 200000,
    usage: dict[str, int] | None = None,
    model: Any = None,
) -> dict[str, Any]:
    if usage is None:
        usage = {
            "input_tokens": 10,
            "output_tokens": 999,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 1000,
        }
    return parse_statusline(
        {
            "model": {"id": "claude-opus-4", "display_name": "Opus"} if model is None else model,
            "context_window": {
                "used_percentage": pct,
                "context_window_size": size,
                "current_usage": usage,
            },
        }
    )


def context_events(router: SessionRouter) -> list[dict[str, Any]]:
    events, _ = router._event_log.page(0, 100, frozenset({"context"}))
    return [e.data for e in events]


def test_snapshot_context_null_initially(tmp_path: Path) -> None:
    router = make_router(tmp_path)
    assert router.context is None
    snap = SessionManager._compose(
        {"session_id": "s"},
        state="ready",
        env={},
        turns_completed=0,
        current=None,
        live={},
        last_activity_at=None,
        context=router.context,
    )
    assert snap["context"] is None
    assert context_events(router) == []


def test_update_fills_snapshot_and_emits_event(tmp_path: Path) -> None:
    router = make_router(tmp_path)
    router.on_statusline(payload())
    ctx = router.context
    assert ctx is not None
    assert ctx["used_percentage"] == 12.5
    assert ctx["window_tokens"] == 200000
    assert ctx["source"] == "statusline"
    assert ctx["updated_at"]
    assert context_events(router) == [ctx]


def test_identical_update_is_deduped(tmp_path: Path) -> None:
    router = make_router(tmp_path)
    router.on_statusline(payload())
    router.on_statusline(payload())
    assert len(context_events(router)) == 1
    router.on_statusline(payload(pct=13.0))
    assert len(context_events(router)) == 2


def test_compact_resets_context(tmp_path: Path) -> None:
    router = make_router(tmp_path)
    router.on_statusline(payload())
    router.reset_context()
    assert router.context is None
    events = context_events(router)
    assert len(events) == 2
    assert events[1]["used_tokens"] is None and events[1]["window_tokens"] is None
    router.reset_context()  # already null: no further event
    assert len(context_events(router)) == 2


def test_null_current_usage_gives_null_context(tmp_path: Path) -> None:
    router = make_router(tmp_path)
    router.on_statusline(payload())
    router.on_statusline(parse_statusline({"context_window": {"current_usage": None}}))
    assert router.context is None
    assert len(context_events(router)) == 2


def test_used_tokens_sum_and_model_id(tmp_path: Path) -> None:
    router = make_router(tmp_path)
    router.on_statusline(payload())
    assert router.context is not None
    assert router.context["used_tokens"] == 10 + 100 + 1000
    assert router.context["model"] == "claude-opus-4"


def test_used_percentage_computed_when_null(tmp_path: Path) -> None:
    router = make_router(tmp_path)
    usage = {"input_tokens": 50000}
    router.on_statusline(payload(pct=None, size=200000, usage=usage))
    assert router.context is not None
    assert router.context["used_percentage"] == 25.0
    router.on_statusline(payload(pct=None, size=1000, usage=usage))
    assert router.context["used_percentage"] == 100.0  # clamped
    router.on_statusline(payload(pct=None, size=None, usage=usage))
    assert router.context["used_percentage"] is None
    assert router.context["window_tokens"] is None
