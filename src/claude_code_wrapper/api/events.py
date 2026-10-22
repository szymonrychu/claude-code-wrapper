"""History and SSE routes over the session event log (D5)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from claude_code_wrapper.api.errors import ApiError
from claude_code_wrapper.events import EVENT_TYPES, MAX_PAGE_LIMIT, Event, EventLog
from claude_code_wrapper.turns import now_iso

router = APIRouter()

CLOSED = "session_closed"


def _envelope(request: Request, ev: Event) -> dict[str, Any]:
    return request.app.state.manager.redactor.redact_obj(asdict(ev))


def _frame(request: Request, ev: Event, *, with_id: bool = True) -> bytes:
    env = _envelope(request, ev)
    head = f"id: {ev.seq}\n" if with_id else ""
    data = json.dumps(env, separators=(",", ":"), ensure_ascii=False)
    return f"{head}event: {ev.type}\ndata: {data}\n\n".encode()


@router.get("/sessions/{session_id}/history")
async def history(
    session_id: str,
    request: Request,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1)] = 100,
    before_seq: Annotated[int | None, Query(ge=0)] = None,
    types: str | None = None,
) -> Any:
    wanted: frozenset[str] | None = None
    if types is not None:
        names = [t.strip() for t in types.split(",") if t.strip()]
        unknown = sorted(set(names) - set(EVENT_TYPES))
        if unknown or not names:
            raise ApiError(400, "invalid_request", f"unknown event types: {', '.join(unknown)}")
        wanted = frozenset(names)
    if before_seq is not None and "after_seq" in request.query_params:
        raise ApiError(400, "invalid_request", "before_seq and after_seq are mutually exclusive")
    log = request.app.state.manager.open_event_log(session_id)
    size = min(limit, MAX_PAGE_LIMIT)
    if before_seq is not None:
        older, more_before = log.page_before(before_seq, size, wanted)
        next_seq = older[-1].seq if older else before_seq
        return {
            "events": [_envelope(request, e) for e in older],
            "next_seq": next_seq,
            "has_more": bool(log.page(next_seq, 1, wanted)[0]),
            "prev_seq": older[0].seq if older and more_before else None,
            "has_more_before": more_before,
        }
    events, more = log.page(after_seq, size, wanted)
    next_seq = events[-1].seq if events else after_seq
    return {
        "events": [_envelope(request, e) for e in events],
        "next_seq": next_seq,
        "has_more": more,
    }


def _replay(log: EventLog, start: int | None, count: int) -> list[Event]:
    if start is None:
        return log.last(count)
    out: list[Event] = []
    cursor = start
    while True:
        page, more = log.page(cursor, MAX_PAGE_LIMIT)
        out.extend(page)
        if not more or not page:
            return out
        cursor = page[-1].seq


def _start_seq(last_event_id: str | None, after_seq: int | None) -> int | None:
    if last_event_id is not None and last_event_id.strip():
        try:
            return max(0, int(last_event_id))
        except ValueError as exc:
            raise ApiError(
                400, "invalid_last_event_id", "Last-Event-ID must be an integer"
            ) from exc
    return after_seq


@router.get("/sessions/{session_id}/events")
async def events(
    session_id: str,
    request: Request,
    after_seq: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    manager = request.app.state.manager
    limits = request.app.state.cfg.limits
    log = manager.open_event_log(session_id)
    start = _start_seq(last_event_id, after_seq)
    sub = log.subscribe()  # before the replay read, so nothing falls in the gap
    replay = _replay(log, start, limits.sse_replay)

    async def stream() -> AsyncIterator[bytes]:
        sent = start if start is not None else 0
        closed = False
        try:
            for ev in replay:
                sent = max(sent, ev.seq)
                yield _frame(request, ev)
                closed = closed or ev.type == CLOSED
            it = sub.__aiter__()
            while not closed:
                try:
                    ev = await asyncio.wait_for(it.__anext__(), limits.sse_heartbeat_seconds)
                except TimeoutError:
                    yield b": ping\n\n"
                    continue
                except StopAsyncIteration:
                    break
                if ev.seq <= sent and ev.type != "error":
                    continue
                sent = max(sent, ev.seq)
                yield _frame(request, ev)
                closed = ev.type == CLOSED
            if not closed:
                tail = log.last(1)
                seq = tail[0].seq if tail else sent
                yield _frame(request, Event(seq, now_iso(), session_id, CLOSED, {}), with_id=False)
        finally:
            sub.unsubscribe()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
