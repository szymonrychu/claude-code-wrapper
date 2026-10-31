"""Turn records, probes and the per-session turn store (`turns.jsonl`).

A turn is one prompt pasted into claude and everything until it ends. Its record is appended to
`turns.jsonl` in full on every change, so the file is an audit trail and the last line per
`turn_id` is the current state. Loading keeps that last line and skips corrupt ones (a crash can
leave a torn final line). A stopped session's store is opened read-only.

`callback_url` serialises as `callbackUrl` everywhere (API dumps and the file), D20.

A turn a service shutdown cut off stays `running` with `interrupted="shutdown"` (D5): the next
process reads it back as `TurnStore.current`, and the resumed session continues or fails it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .timeutil import now_iso  # noqa: F401  (re-exported: the one service timestamp)

log = logging.getLogger(__name__)

Origin = Literal["client", "initial", "agent", "peer"]
#: How a message reaches a session: `idle` waits for no turn to be running, `regular` is typed
#: into a running turn for claude to queue, `priority` interrupts the running turn first.
Lane = Literal["idle", "regular", "priority"]
LANES: tuple[str, ...] = get_args(Lane)
TurnState = Literal["running", "completed", "failed", "interrupted", "timeout"]
ProbeState = Literal["pending", "delivered", "answered"]
#: Why a `running` turn has no claude working on it: the service shut down under it.
Interruption = Literal["shutdown"]

TERMINAL_STATES: tuple[str, ...] = ("completed", "failed", "interrupted", "timeout")
SUMMARY_FIELDS: tuple[str, ...] = (
    "turn_id",
    "origin",
    "state",
    "started_at",
    "completed_at",
    "stop_reason",
)
_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"

#: The nanosecond clock ids are derived from (a module attribute so tests can replace it).
_ns_clock: Callable[[], int] = time.time_ns
_id_lock = threading.Lock()
_last_ns = 0


def base36(value: int) -> str:
    if value < 0:
        raise ValueError("base36 takes a non-negative integer")
    out = ""
    while True:
        value, digit = divmod(value, 36)
        out = _DIGITS[digit] + out
        if not value:
            return out


def _next_ns() -> int:
    """The clock in nanoseconds, bumped past the last value handed out (repeats, steps back)."""
    global _last_ns
    with _id_lock:
        _last_ns = max(_ns_clock(), _last_ns + 1)
        return _last_ns


def new_turn_id() -> str:
    """`turn-<base36 nanoseconds>`, strictly increasing within the process."""
    return "turn-" + base36(_next_ns())


#: Characters of the answering assistant text kept on a probe.
PROBE_ANSWER_MAX = 2000


def new_probe_id() -> str:
    """`probe-<base36 nanoseconds>`, from the same strictly increasing source as turn ids."""
    return "probe-" + base36(_next_ns())


def new_message_id() -> str:
    """`msg-<base36 nanoseconds>`: a message typed into a running turn (a lane delivery)."""
    return "msg-" + base36(_next_ns())


class TurnRecord(BaseModel):
    """One turn. Mutated in place by its session and `put` to the store after every change."""

    model_config = ConfigDict(
        extra="ignore", populate_by_name=True, serialize_by_alias=True, validate_assignment=True
    )

    turn_id: str
    session_id: str
    origin: Origin = "client"
    state: TurnState = "running"
    text: str = ""
    final_text: str | None = None
    stop_reason: str | None = None
    usage: dict[str, Any] | None = None
    model_tokens: dict[str, Any] | None = None
    error: str | None = None
    #: Set while the turn waits for its session's resume after a shutdown cut it off.
    interrupted: Interruption | None = None
    callback_url: str | None = Field(default=None, alias="callbackUrl")
    started_at: str
    completed_at: str | None = None
    last_activity_at: str | None = None
    stall_suspected_since: str | None = None
    tool_calls: int = 0
    annotations: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def summary(self) -> dict[str, Any]:
        """The list-view fields (`SUMMARY_FIELDS`)."""
        return {name: getattr(self, name) for name in SUMMARY_FIELDS}


#: A turn's annotations may not exceed this many bytes serialised.
ANNOTATIONS_MAX_BYTES = 16 * 1024
#: A turn's client-supplied metadata may not exceed this many bytes serialised.
METADATA_MAX_BYTES = 16 * 1024


def merged_annotations(current: dict[str, Any], new: dict[str, Any]) -> dict[str, Any] | None:
    """`current` shallow-merged with `new` (later keys win); None if the result is over 16 KiB."""
    merged = {**current, **new}
    if len(json.dumps(merged, ensure_ascii=False).encode()) > ANNOTATIONS_MAX_BYTES:
        return None
    return merged


class Probe(BaseModel):
    """A liveness probe: text pasted without taking the turn slot, answered with the alive token."""

    model_config = ConfigDict(validate_assignment=True)

    probe_id: str
    state: ProbeState = "pending"
    text: str
    created_at: str
    delivered_at: str | None = None
    answered_at: str | None = None
    #: The main-chain assistant text that answered the probe, capped at PROBE_ANSWER_MAX.
    answer: str | None = None


class StoreReadOnly(RuntimeError):
    """`put` on a store opened with `readonly=True` (a stopped session's history)."""


class TurnStore:
    """The turns of one session, backed by an append-only JSON-lines file.

    `get`, `list` and `current` serve copies of what was last `put`, so a record changed but not
    yet saved is not visible here. `list()` is in start order: the order turn ids first appear.
    """

    def __init__(self, path: str | os.PathLike[str], readonly: bool = False) -> None:
        self.path = os.fspath(path)
        self.readonly = readonly
        self._records: dict[str, TurnRecord] = {}
        self._needs_newline = False
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = f.read()
        except FileNotFoundError:
            return
        self._needs_newline = bool(data) and not data.endswith("\n")
        for n, line in enumerate(data.splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = TurnRecord.model_validate(json.loads(line))
            except (ValueError, ValidationError) as exc:
                log.warning("%s:%d: skipping corrupt turn line (%s)", self.path, n, exc)
                continue
            self._records[record.turn_id] = record

    def put(self, record: TurnRecord) -> None:
        """Append the full record as one JSON line and make it the current state of its turn."""
        if self.readonly:
            raise StoreReadOnly(f"{self.path} is read-only")
        line = record.model_dump_json(by_alias=True) + "\n"
        if self._needs_newline:  # never glue a new record onto a torn line
            line = "\n" + line
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)
        self._needs_newline = False
        self._records[record.turn_id] = record.model_copy(deep=True)

    def refresh(self, record: TurnRecord) -> None:
        """Make `record` the served state of its turn without appending a line.

        For high-frequency fields (`last_activity_at`) whose every change need not reach
        disk: readers see the new value at once, and the next `put` writes it.
        """
        if self.readonly:
            raise StoreReadOnly(f"{self.path} is read-only")
        self._records[record.turn_id] = record.model_copy(deep=True)

    def get(self, turn_id: str) -> TurnRecord | None:
        record = self._records.get(turn_id)
        return record.model_copy(deep=True) if record is not None else None

    def list(self) -> list[TurnRecord]:
        return [r.model_copy(deep=True) for r in self._records.values()]

    @property
    def current(self) -> TurnRecord | None:
        """The running turn, if any (the latest one should a crash have left several)."""
        for record in reversed(self._records.values()):
            if record.state == "running":
                return record.model_copy(deep=True)
        return None
