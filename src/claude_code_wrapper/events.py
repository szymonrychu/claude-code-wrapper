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
    "prompt_queued",
    "prompt_delivered",
    "prompt_dropped",
    "assistant_text",
    "thinking",
    "usage",
    "tool_call",
    "tool_result",
    "tool_denied",
    "question",
    "subagent_started",
    "subagent_stopped",
    "subagent_linked",
    "interrupted",
    "stall_suspected",
    "probe",
    "hook_result",
    "command_queued",
    "command_started",
    "command_completed",
    "command_failed",
    "compacting",
    "compacted",
    "context",
    "error",
    "permission_mode_unavailable",
    "waiting_started",
    "waiting_ended",
    "bootstrap_step",
    "destroy_output",
    "peer_message_received",
    "peer_message_sent",
    "peer_message_delivered",
    "peer_member_joined",
    "peer_member_left",
    "note_received",
    "note_delivered",
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

    def _read_file_reversed(self, chunk_size: int = 65536) -> Iterator[Event]:
        """Events newest-first, reading the file backwards in chunks (lazy)."""
        try:
            fh = self.path.open("rb")
        except OSError:
            return
        with fh:
            pos = fh.seek(0, os.SEEK_END)
            carry = b""
            while pos > 0:
                step = min(chunk_size, pos)
                pos -= step
                fh.seek(pos)
                parts = (fh.read(step) + carry).split(b"\n")
                carry = parts[0] if pos > 0 else b""
                lines = parts[1:] if pos > 0 else parts
                for raw in reversed(lines):
                    ev = self._parse_line(raw)
                    if ev is not None:
                        yield ev

    @staticmethod
    def _parse_line(raw: bytes) -> Event | None:
        try:
            return Event.from_json(raw.decode("utf-8", errors="replace"))
        except (ValueError, KeyError, TypeError):
            return None

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

    def page(
        self, after_seq: int = 0, limit: int = 100, types: frozenset[str] | None = None
    ) -> tuple[list[Event], bool]:
        limit = max(1, min(limit, MAX_PAGE_LIMIT))
        if self._ring and self._ring[0].seq <= after_seq + 1 and not self.readonly:
            source: Iterator[Event] = iter(list(self._ring))
        elif not self._ring and after_seq >= self._seq:
            return [], False
        else:
            source = self._read_file()
        out: list[Event] = []
        for ev in source:
            if ev.seq <= after_seq or (types is not None and ev.type not in types):
                continue
            if len(out) == limit:
                return out, True
            out.append(ev)
        return out, False

    def page_before(
        self, before_seq: int, limit: int = 100, types: frozenset[str] | None = None
    ) -> tuple[list[Event], bool]:
        """Newest `limit` events with seq < before_seq, ascending, and whether older ones exist."""
        limit = max(1, min(limit, MAX_PAGE_LIMIT))

        def newest(source: Iterator[Event]) -> list[Event]:
            # source is newest-first; stop once limit + 1 matches are collected
            tail: list[Event] = []
            for ev in source:
                if ev.seq < before_seq and (types is None or ev.type in types):
                    tail.append(ev)
                    if len(tail) > limit:
                        break
            tail.reverse()
            return tail

        found: list[Event] = []
        ring = list(self._ring)
        if ring and not self.readonly:
            found = newest(reversed(ring))
        # The ring only holds the newest events: use it when it spans back to seq 1 or
        # already holds a full page plus one; otherwise the file is the source of truth.
        if not ring or (len(found) <= limit and ring[0].seq > 1) or self.readonly:
            found = newest(self._read_file_reversed())
        more = len(found) > limit
        return found[-limit:], more

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
