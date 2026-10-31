"""Per-session peer inbox: queueing, bounds, rate limit and delivery by lane (D7, D9, D14).

`put` queues a peer message or notice. On an idle session (ready, no turn, no command runner,
no idle-runner command queued) delivery takes the head; while a turn runs it takes the first
message whose lane is not `idle` (`regular`, the default, or `priority`), when the running turn
can take a message (`Session.check_queueable`). Either way it goes through
`SessionRouter.deliver` with the item's lane and origin `peer`, so the onTurn hook may veto it:
a new turn, or the text typed into the running turn (`regular`) or a new turn after the running
one is interrupted (`priority`). Notices are always `idle`. Nothing here writes to the PTY.
`deliver` awaits the onTurn hook, so the session can change in between; a `TurnRejected` then
keeps the item queued and the next ready state delivers it.

At most one delivery attempt is in flight. A `kick` while one is scheduled or running sets a
pending flag, honoured when the attempt ends; an attempt that ends with items left on a ready
session runs again on the next loop pass, so a queued item is never stranded. Delivery is at
most once: the inbox lives in memory only.

Outcomes (D8): a `message` item that was vetoed (`rejected`) or still queued at `close`
(`dropped`) always reaches `on_outcome`; a delivered one reaches it only with `notify_when_idle`,
once its handling turn is terminal (for a message typed into a running turn: the turn that read
it, `message_delivered`). Notices never produce outcomes. Each call runs as an
inbox-owned background task, so a slow sender never holds up the Stop answer, a delivery attempt
or `close`; `drain` bounds the wait for them on shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from xml.sax.saxutils import escape

from .events import Event as LogEvent
from .metrics import Metrics
from .peer_client import PeerError
from .redact import Redactor
from .router import TurnVetoed
from .session import TurnRejected
from .turns import Lane, TurnRecord

if TYPE_CHECKING:
    from .router import SessionRouter

log = logging.getLogger(__name__)

#: Accepted `(from_address, msg_id, kind)` remembered for dedupe, per session (D14).
DEDUPE_WINDOW = 256
#: The sliding window of the per-sender rate limit, in seconds.
RATE_WINDOW_SECONDS = 60.0
#: Distinct sender keys the per-sender rate map may hold; the oldest is evicted.
MAX_RATE_SENDERS = 1024
#: Seconds between re-checks while an idle-runner command is queued but not yet started.
_QUEUED_COMMAND_POLL = 0.05

Kind = Literal["message", "notice"]
#: What happened to a message: `on_outcome`'s status argument.
OutcomeStatus = Literal["delivered", "rejected", "dropped"]
#: Seconds `drain` gives cancelled outcome tasks to unwind.
_CANCEL_GRACE = 0.05


@dataclass
class InboxItem:
    kind: Kind
    from_address: str
    msg_id: str
    text: str
    notify_when_idle: bool = False
    #: Notices: delivered, rejected or dropped.
    status: str | None = None
    #: Notices with status delivered: the handling turn's terminal state.
    state: str | None = None
    #: Notices: why the message was not delivered. Messages: the onTurn veto reason.
    reason: str | None = None
    #: Messages: how it reaches the session (notices are always delivered `idle`).
    lane: Lane = "regular"


#: `on_outcome(item, status, record)`; `record` is the handling turn for `delivered`, else None.
OnOutcome = Callable[["InboxItem", OutcomeStatus, TurnRecord | None], Awaitable[None]]


@dataclass
class PutResult:
    depth: int
    duplicate: bool


def _attr(value: str | None) -> str:
    return escape(value or "", {'"': "&quot;"})


def frame_message(item: InboxItem) -> str:
    """The D7 frame; the body and the attribute values are XML-escaped."""
    sender = _attr(item.from_address)
    return (
        f'<agent-message from="{sender}" msg_id="{_attr(item.msg_id)}">\n'
        f"{escape(item.text)}\n"
        "</agent-message>\n"
        f'Reply with mcp__ccw__send_message to "{sender}" only if a reply is needed.'
    )


def frame_notice(item: InboxItem) -> str:
    """The D8 frame: an idle notice for `delivered`, a delivery failure otherwise."""
    sender, msg_id = _attr(item.from_address), _attr(item.msg_id)
    if item.status == "delivered":
        return (
            f'<peer-idle-notice from="{sender}" msg_id="{msg_id}" state="{_attr(item.state)}"/>\n'
            f"The peer finished the turn that handled your message {msg_id} and is idle."
        )
    status = _attr(item.status)
    return (
        f'<peer-delivery-failed from="{sender}" msg_id="{msg_id}" status="{status}" '
        f'reason="{_attr(item.reason)}"/>\n'
        f"Your message {msg_id} was not delivered ({status})."
    )


def frame(item: InboxItem) -> str:
    return frame_message(item) if item.kind == "message" else frame_notice(item)


class PeerInbox:
    """One session's FIFO of peer messages and notices, delivered one turn at a time."""

    def __init__(
        self,
        router: SessionRouter,
        *,
        max_items: int,
        max_per_minute: int,
        max_total_per_minute: int = 120,
        metrics: Metrics | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_outcome: OnOutcome | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self._router = router
        self._on_outcome = on_outcome
        #: Redacts outcome failures before they are logged; defaults to the router's.
        self._redactor: Redactor = redactor or getattr(router, "_redactor", None) or Redactor()
        self._outcomes: set[asyncio.Task[None]] = set()
        self._max_items = max_items
        self._max_per_minute = max_per_minute
        self._max_total_per_minute = max_total_per_minute
        self._total: deque[float] = deque()
        self._metrics = metrics
        self._clock = clock
        self._items: deque[InboxItem] = deque()
        self._seen: deque[tuple[str, str, str]] = deque()
        self._seen_set: set[tuple[str, str, str]] = set()
        self._rate: dict[str, deque[float]] = {}
        #: turn_id -> the items that turn handles, until the turn finishes.
        self._turns: dict[str, list[InboxItem]] = {}
        #: message_id -> a message typed into a running turn, until its marker is read.
        self._typed: dict[str, InboxItem] = {}
        self._scheduled = False
        self._pending = False
        self._closed = False
        self._task: asyncio.Task[None] | None = None

    @property
    def depth(self) -> int:
        return len(self._items)

    @property
    def closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------------ queueing

    def put(self, item: InboxItem) -> PutResult:
        """Queue `item`; raises `PeerError` rate_limited or inbox_full (D7, D14)."""
        key = (item.from_address, item.msg_id, item.kind)
        if key in self._seen_set:
            self._count_received(item, "duplicate")
            self.kick()
            return PutResult(depth=len(self._items), duplicate=True)
        if item.kind == "message":
            if not self._within_total_rate():
                self._count_received(item, "rate_limited")
                raise PeerError(
                    "rate_limited",
                    f"more than {self._max_total_per_minute} messages a minute in total",
                )
            if not self._within_rate(item.from_address):
                self._count_received(item, "rate_limited")
                raise PeerError(
                    "rate_limited",
                    f"more than {self._max_per_minute} messages a minute from {item.from_address}",
                )
        if len(self._items) >= self._max_items:
            self._count_received(item, "inbox_full")
            raise PeerError("inbox_full", f"the inbox holds {self._max_items} items")
        self._remember(key)
        if item.kind == "message":
            now = self._clock()
            self._total.append(now)
            if item.from_address not in self._rate and len(self._rate) >= MAX_RATE_SENDERS:
                del self._rate[next(iter(self._rate))]  # oldest sender key goes
            self._rate.setdefault(item.from_address, deque()).append(now)
        self._items.append(item)
        depth = len(self._items)
        self._router.emit_event(
            "peer_message_received",
            {
                "msg_id": item.msg_id,
                "from": item.from_address,
                "kind": item.kind,
                "bytes": len(item.text.encode()),
                "message": item.text,
                "queued": depth,
            },
        )
        self._count_received(item, "accepted")
        self.kick()
        return PutResult(depth=depth, duplicate=False)

    def _within_total_rate(self) -> bool:
        horizon = self._clock() - RATE_WINDOW_SECONDS
        while self._total and self._total[0] <= horizon:
            self._total.popleft()
        return len(self._total) < self._max_total_per_minute

    def _within_rate(self, sender: str) -> bool:
        stamps = self._rate.get(sender)
        if stamps is None:
            return True
        horizon = self._clock() - RATE_WINDOW_SECONDS
        while stamps and stamps[0] <= horizon:
            stamps.popleft()
        if not stamps:
            del self._rate[sender]  # bounded: idle senders leave the table
            return True
        return len(stamps) < self._max_per_minute

    def _remember(self, key: tuple[str, str, str]) -> None:
        self._seen.append(key)
        self._seen_set.add(key)
        if len(self._seen) > DEDUPE_WINDOW:
            self._seen_set.discard(self._seen.popleft())

    # ------------------------------------------------------------------ delivery

    def kick(self) -> None:
        """Schedule one delivery attempt on the next loop pass (a no-op after `close`)."""
        if self._closed:
            return
        if self._scheduled or (self._task is not None and not self._task.done()):
            self._pending = True
            return
        try:
            asyncio.get_running_loop().call_soon(self._start)
        except RuntimeError:
            return  # no loop: the next kick from inside one schedules it
        self._scheduled = True

    def _start(self) -> None:
        self._scheduled = False
        if self._closed:
            return
        self._pending = False  # this attempt covers every kick so far
        self._task = asyncio.ensure_future(self._attempt())
        self._task.add_done_callback(self._attempt_done)

    def _attempt_done(self, task: asyncio.Task[None]) -> None:
        if self._task is task:
            self._task = None
        if not task.cancelled() and task.exception() is not None:
            log.error("peer inbox delivery attempt failed", exc_info=task.exception())
        if self._closed:
            return
        if self._pending or (self._items and (self._session_ready() or self._busy_pick())):
            self._pending = False
            self.kick()

    def _session_ready(self) -> bool:
        session = self._router.session
        return session.state == "ready" and session.current_turn is None

    def _idle_command_queued(self) -> bool:
        return any(r.turn_id is None for r in self._router.commands.pending())

    @staticmethod
    def _lane(item: InboxItem) -> str:
        return item.lane if item.kind == "message" else "idle"

    def _busy_pick(self) -> InboxItem | None:
        """The first non-idle message, when the running turn can take one now."""
        item = next((i for i in self._items if self._lane(i) != "idle"), None)
        if item is None:
            return None
        try:
            self._router.session.check_queueable()
        except TurnRejected:
            return None
        return item

    async def _attempt(self) -> None:
        router = self._router
        while True:
            if self._closed or not self._items:
                return
            if not self._session_ready():
                picked = self._busy_pick()
                if picked is None:
                    return
                break
            if router.runner_active:
                await router.wait_runner_idle()  # a runner finishing on a ready session
                continue
            if self._idle_command_queued():
                # The idle runner starts on its own loop pass and takes the slot (busy).
                await asyncio.sleep(_QUEUED_COMMAND_POLL)
                continue
            picked = self._items[0]
            break
        item = picked
        peer = {"from": item.from_address, "msg_id": item.msg_id, "kind": item.kind}
        try:
            delivery = await router.deliver(
                frame(item), lane=self._lane(item), origin="peer", metadata={"peer": peer}
            )
        except TurnRejected as exc:
            log.debug("peer inbox: %s stays queued (%s)", item.msg_id, exc.reason)
            return
        except TurnVetoed as exc:
            self._pop(item)
            item.reason = exc.reason
            self._emit_delivered(item, None, "rejected")
            self._outcome(item, "rejected", None)
            return
        except Exception:
            log.exception("peer inbox: delivering %s failed; it stays queued", item.msg_id)
            return
        self._pop(item)
        message = delivery.message
        if message is not None:
            self._typed[message.message_id] = item
            turn_id = message.turn_id
        else:
            assert delivery.record is not None
            turn_id = delivery.record.turn_id
            self._turns.setdefault(turn_id, []).append(item)
        self._emit_delivered(item, turn_id, "delivered")

    def _pop(self, item: InboxItem) -> None:
        with contextlib.suppress(ValueError):
            self._items.remove(item)

    def _emit_delivered(self, item: InboxItem, turn_id: str | None, result: str) -> LogEvent:
        if self._metrics is not None:
            self._metrics.peer_delivered(item.kind, result)
        return self._router.emit_event(
            "peer_message_delivered",
            {
                "msg_id": item.msg_id,
                "from": item.from_address,
                "kind": item.kind,
                "turn_id": turn_id,
                "result": result,
            },
        )

    def _count_received(self, item: InboxItem, result: str) -> None:
        if self._metrics is not None:
            self._metrics.peer_received(item.kind, result)

    # ------------------------------------------------------------------ outcomes

    def delivered_turn(self, turn_id: str) -> InboxItem | None:
        """The first item the running turn `turn_id` handles, if it handles a peer message."""
        items = self._turns.get(turn_id)
        return items[0] if items else None

    def message_delivered(self, message_id: str, turn_id: str) -> None:
        """A peer message typed into a running turn was read in the turn `turn_id`."""
        item = self._typed.pop(message_id, None)
        if item is not None:
            self._turns.setdefault(turn_id, []).append(item)

    def message_dropped(self, message_id: str) -> None:
        """A peer message typed into a running turn will never be read: outcome `dropped`."""
        item = self._typed.pop(message_id, None)
        if item is not None:
            self._outcome(item, "dropped", None)

    def turn_finished(self, record: TurnRecord) -> None:
        """A turn reached a terminal state (synchronous: called on the Stop hook path).

        Each message it handled that asked for `notify_when_idle` gets its `delivered` outcome
        now, with the handling turn's record, in the background.
        """
        for item in self._turns.pop(record.turn_id, []):
            if item.notify_when_idle:
                self._outcome(item, "delivered", record)

    def _outcome(self, item: InboxItem, status: OutcomeStatus, record: TurnRecord | None) -> None:
        """Run `on_outcome` for a message item as a background task; notices never notify."""
        if self._on_outcome is None or item.kind != "message":
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.warning("peer inbox: no event loop; outcome %s of %s lost", status, item.msg_id)
            return
        task = loop.create_task(self._run_outcome(item, status, record))
        self._outcomes.add(task)
        task.add_done_callback(self._outcomes.discard)

    async def _run_outcome(
        self, item: InboxItem, status: OutcomeStatus, record: TurnRecord | None
    ) -> None:
        assert self._on_outcome is not None
        try:
            await self._on_outcome(item, status, record)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(
                "peer inbox: outcome %s of %s failed: %s",
                status,
                item.msg_id,
                self._redactor.redact(f"{type(exc).__name__}: {exc}"),
            )

    async def drain(self, timeout: float) -> None:
        """Wait up to `timeout` seconds for outstanding outcome tasks, then cancel the rest."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while pending := [t for t in self._outcomes if not t.done()]:
            left = deadline - loop.time()
            if left <= 0:
                for task in pending:
                    task.cancel()
                await asyncio.wait(pending, timeout=_CANCEL_GRACE)
                return
            await asyncio.wait(pending, timeout=left)

    def close(self) -> list[InboxItem]:
        """Stop scheduling and drop the queued items, emitting result `dropped` for each.

        Each dropped message also gets its `dropped` outcome (in the background; `drain` waits).

        An attempt still in flight is cancelled: the router closes after the session stopped,
        so that attempt can only be waiting in the onTurn hook or failing on a closed PTY.
        """
        if self._closed:
            return []
        self._closed = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
        dropped = list(self._items)
        self._items.clear()
        for item in dropped:
            self._emit_delivered(item, None, "dropped")
            self._outcome(item, "dropped", None)
        return dropped
