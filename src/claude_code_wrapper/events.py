"""Per-session durable event log with RAM ring, paging and live fan-out (D5)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections import deque
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from claude_code_wrapper.timeutil import now_iso

log = logging.getLogger(__name__)

EVENT_TYPES: list[str] = [
    "session_state",
    "turn_started",
    "user_message",
    "turn_rejected",
    "turn_completed",
    "turn_failed",
    "turn_continued",
    "turn_deferred",
    "turn_resumed",
    "assistant_text",
    "thinking",
    "usage",
    "tool_call",
    "tool_result",
    "tool_denied",
    "question",
    "subagent_started",
    "subagent_stopped",
    "interrupted",
    "stall_suspected",
    "probe",
    "hook_result",
    "command_queued",
    "command_started",
    "command_completed",
    "command_failed",
    "compacted",
    "error",
    "destroy_output",
    "peer_message_received",
    "peer_message_sent",
    "peer_message_delivered",
    "peer_member_joined",
    "peer_member_left",
    "session_closed",
]

MAX_PAGE_LIMIT = 1000
SUBSCRIBER_QUEUE_SIZE = 1000


@dataclass(frozen=True)
class Event:
    seq: int
    ts: str
    session_id: str
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> Event:
        d = json.loads(line)
        return cls(
            int(d["seq"]), str(d["ts"]), str(d["session_id"]), str(d["type"]), dict(d["data"])
        )


class Subscription:
    """Async iterator over live events, backed by a bounded queue."""

    def __init__(self, owner: EventLog | None = None) -> None:
        self._owner = owner
        self._queue: asyncio.Queue[Event] = asyncio.Queue(SUBSCRIBER_QUEUE_SIZE)
        self._wake = asyncio.Event()
        self._ended = owner is None
        self._lagged = False
        self._error: Event | None = None

    def _put(self, ev: Event) -> bool:
        if self._ended:
            return True
        try:
            self._queue.put_nowait(ev)
        except asyncio.QueueFull:
            self._lagged = True
            self._error = Event(
                ev.seq, ev.ts, ev.session_id, "error", {"message": "subscriber lagged"}
            )
            self._end()
            return False
        self._wake.set()
        return True

    def _end(self) -> None:
        self._ended = True
        self._wake.set()

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Event:
        while True:
            if not self._queue.empty():
                return self._queue.get_nowait()
            if self._ended:
                if self._error is not None:
                    err, self._error = self._error, None
                    return err
                raise StopAsyncIteration
            self._wake.clear()
            await self._wake.wait()

    def unsubscribe(self) -> None:
        if self._owner is not None:
            self._owner.unsubscribe(self)


class EventLog:
    def __init__(
        self,
        path: str | Path,
        session_id: str,
        ring_size: int = 1000,
        max_bytes: int = 52428800,
        readonly: bool = False,
    ) -> None:
        self.path = Path(path)
        self.session_id = session_id
        self.max_bytes = max_bytes
        self.readonly = readonly
        self._ring: deque[Event] = deque(maxlen=ring_size)
        self._subs: set[Subscription] = set()
        self._warned = False
        self._closed = False
        self._fh: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        with contextlib.suppress(RuntimeError):
            self._loop = asyncio.get_running_loop()

        events = list(self._read_file())
        self._seq = events[-1].seq if events else 0
        for ev in events[-ring_size:]:
            self._ring.append(ev)
        if not readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._open()

    def _open(self) -> None:
        try:
            needs_newline = False
            with contextlib.suppress(OSError), self.path.open("rb") as rf:
                rf.seek(0, os.SEEK_END)
                if rf.tell() > 0:
                    rf.seek(-1, os.SEEK_END)
                    needs_newline = rf.read(1) != b"\n"
            self._fh = self.path.open("a", encoding="utf-8")
            if needs_newline:
                self._fh.write("\n")
        except OSError as exc:
            self._fh = None
            self._warn(exc)

    def _warn(self, exc: Exception) -> None:
        if not self._warned:
            self._warned = True
            log.error("event log write failed for %s: %s", self.path, exc)

    def _read_file(self) -> Iterator[Event]:
        try:
            fh = self.path.open(encoding="utf-8", errors="replace")
        except OSError:
            return
        with fh:
            for line in fh:
                try:
                    yield Event.from_json(line)
                except (ValueError, KeyError, TypeError):
                    continue

    def append(self, type: str, data: dict[str, Any] | None = None) -> Event:
        if self.readonly:
            raise RuntimeError("event log is read-only")
        if type not in EVENT_TYPES:
            raise ValueError(f"unknown event type: {type}")
        self._seq += 1
        ev = Event(self._seq, now_iso(), self.session_id, type, dict(data or {}))
        self._ring.append(ev)
        self._write(ev)
        for sub in list(self._subs):
            if not sub._put(ev):
                self._subs.discard(sub)
        return ev

    def _write(self, ev: Event) -> None:
        try:
            if self._fh is None:
                raise OSError("log file not open")
            self._fh.write(ev.to_json() + "\n")
            self._fh.flush()
            if self.path.stat().st_size > self.max_bytes:
                self._rotate()
        except Exception as exc:  # noqa: BLE001 - never raise from append
            self._warn(exc)

    def _rotate(self) -> None:
        self._fh.close()
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
            keep = lines[len(lines) // 2 :]
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
        finally:
            self._open()

    def append_threadsafe(self, type: str, data: dict[str, Any] | None = None) -> None:
        if type not in EVENT_TYPES:
            raise ValueError(f"unknown event type: {type}")
        if self._loop is None or self._loop.is_closed():
            self.append(type, data)
            return
        self._loop.call_soon_threadsafe(self.append, type, data)

    def page(self, after_seq: int = 0, limit: int = 100) -> tuple[list[Event], bool]:
        limit = max(1, min(limit, MAX_PAGE_LIMIT))
        if self._ring and self._ring[0].seq <= after_seq + 1 and not self.readonly:
            source: Iterator[Event] = iter(self._ring)
        elif not self._ring and after_seq >= self._seq:
            return [], False
        else:
            source = self._read_file()
        out: list[Event] = []
        for ev in source:
            if ev.seq <= after_seq:
                continue
            if len(out) == limit:
                return out, True
            out.append(ev)
        return out, False

    def last(self, n: int) -> list[Event]:
        if n <= 0:
            return []
        return list(self._ring)[-n:]

    def subscribe(self) -> Subscription:
        if self.readonly or self._closed:
            return Subscription(None)
        sub = Subscription(self)
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subs.discard(sub)
        sub._end()

    def close(self) -> None:
        self._closed = True
        for sub in list(self._subs):
            sub._end()
        self._subs.clear()
        if self._fh is not None:
            with contextlib.suppress(OSError):
                self._fh.close()
            self._fh = None
