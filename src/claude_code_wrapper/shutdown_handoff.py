"""Pre-shutdown notice and handoff turns (`shutdown.handoff`).

On SIGTERM, before `SessionManager.shutdown`, every live session is warned with a notice turn
and then asked for a handoff turn, both on the regular lane (a running turn is never
interrupted: the text is typed into it). The routine reports each session's handoff on its
event stream (`shutdown_started`, `shutdown_handoff`) while the HTTP servers are still up. It
never raises and never waits longer than `notice_seconds + handoff_seconds`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from .config import ShutdownHandoffConfig
from .router import Delivery, SessionRouter
from .turns import TurnRecord

if TYPE_CHECKING:
    from .manager import SessionManager

log = logging.getLogger(__name__)

#: Note key the notice is stored under when notes are enabled.
NOTICE_KEY = "shutdown_notice"
#: Seconds between idle and completion checks.
POLL_SECONDS = 0.05
#: Turn states that end a turn without completing it.
_FAILED = ("failed", "interrupted", "timeout")


def _idle(router: SessionRouter) -> bool:
    """No running turn, no queued command and no running client command."""
    return (
        router.session.state == "ready"
        and router.session.current_turn is None
        and not router.runner_active
        and router.commands.next() is None
    )


async def _notice_wait(
    router: SessionRouter, cfg: ShutdownHandoffConfig, clock: Callable[[], float]
) -> None:
    """Until `notice_seconds` passed, or earlier once the session stayed idle."""
    deadline = clock() + cfg.notice_seconds
    idle_since: float | None = None
    while (now := clock()) < deadline:
        if _idle(router):
            idle_since = now if idle_since is None else idle_since
            if now - idle_since >= cfg.idle_proceed_seconds:
                return
        else:
            idle_since = None
        await asyncio.sleep(min(POLL_SECONDS, max(deadline - now, 0)))


async def _handoff_result(
    router: SessionRouter, delivery: Delivery, deadline: float, clock: Callable[[], float]
) -> tuple[str | None, str, str | None]:
    """(turn_id, status, final_text) of the handoff, waiting for its turn until `deadline`."""
    message = delivery.message
    while True:
        record: TurnRecord | None = delivery.record
        if message is not None:  # typed into a running turn: its own turn is the one that read it
            turn_id = message.delivered_turn_id
            record = router.turns.get(turn_id) if turn_id and router.turns else None
        if record is not None and record.terminal:
            if record.state == "completed":
                return record.turn_id, "completed", record.final_text
            return record.turn_id, "failed", None
        if clock() >= deadline:
            if record is None:
                return None, "not_started", None
            return record.turn_id, "running", None
        await asyncio.sleep(POLL_SECONDS)


async def _handoff_one(
    manager: SessionManager,
    session_id: str,
    cfg: ShutdownHandoffConfig,
    clock: Callable[[], float],
) -> None:
    router = manager.router(session_id)
    router.emit_event("shutdown_started", {})
    try:
        if manager.cfg.notes.enabled:
            manager.note_store(session_id).put(cfg.notice_prompt, key=NOTICE_KEY, source="wrapper")
        await asyncio.wait_for(
            router.deliver(cfg.notice_prompt, lane="regular"), timeout=max(cfg.notice_seconds, 1)
        )
    except Exception as exc:  # the notice is best effort; the handoff still runs
        log.warning("session %s: shutdown notice not delivered: %s", session_id, exc)
    await _notice_wait(router, cfg, clock)
    deadline = clock() + cfg.handoff_seconds
    turn_id, status, final_text = None, "not_started", None
    try:
        delivery = await asyncio.wait_for(
            router.deliver(cfg.handoff_prompt, lane="regular"), timeout=max(cfg.handoff_seconds, 1)
        )
        turn_id, status, final_text = await _handoff_result(router, delivery, deadline, clock)
    except Exception as exc:
        log.warning("session %s: shutdown handoff not run: %s", session_id, exc)
    router.emit_event(
        "shutdown_handoff", {"turn_id": turn_id, "status": status, "final_text": final_text}
    )


async def run_shutdown_handoff(
    manager: SessionManager,
    cfg: ShutdownHandoffConfig,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Per live session, concurrently: emit `shutdown_started`; store the notice as note
    `shutdown_notice` (notes enabled) and submit it as a regular-lane turn; wait until
    `notice_seconds` passed, or earlier once the session stayed idle `idle_proceed_seconds`;
    submit the handoff prompt as a regular-lane turn and wait for it to end, at most
    `handoff_seconds`; emit `shutdown_handoff` `{turn_id, status, final_text}` (status
    `completed`, `failed`, `running` or `not_started`; `final_text` only when `completed`).
    Never raises; never waits longer than `notice_seconds + handoff_seconds` in total."""

    async def one(session_id: str) -> None:
        try:
            await asyncio.wait_for(
                _handoff_one(manager, session_id, cfg, clock),
                timeout=cfg.notice_seconds + cfg.handoff_seconds + 1,
            )
        except Exception as exc:
            log.warning("session %s: shutdown handoff failed: %s", session_id, exc)

    await asyncio.gather(*(one(sid) for sid in manager.active_ids()), return_exceptions=True)
