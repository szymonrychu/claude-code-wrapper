"""Completion signals for typed slash commands: a bounded, awaitable history of what happened."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

SignalKind = Literal[
    "pre_compact", "post_compact", "compact_boundary", "command_invoked", "command_output"
]
HISTORY = 64


@dataclass(frozen=True)
class Signal:
    kind: str
    data: Mapping[str, Any] = field(default_factory=dict)
    seq: int = 0


class SignalBus:
    def __init__(self) -> None:
        self._history: deque[Signal] = deque(maxlen=HISTORY)
        self._seq = 0
        self._changed = asyncio.Event()

    def mark(self) -> int:
        """The seq of the latest signal; wait(after=mark) sees only later ones."""
        return self._seq

    def publish(self, kind: str, data: Mapping[str, Any] | None = None) -> Signal:
        self._seq += 1
        sig = Signal(kind, dict(data or {}), self._seq)
        self._history.append(sig)
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()
        return sig

    def _find(self, predicate: Callable[[Signal], bool], after: int) -> Signal | None:
        for sig in self._history:
            if sig.seq > after and predicate(sig):
                return sig
        return None

    async def wait(
        self, predicate: Callable[[Signal], bool], *, after: int, timeout: float
    ) -> Signal | None:
        """First signal with seq > after matching predicate (published already or later)."""
        deadline = time.monotonic() + timeout
        while True:
            changed = self._changed
            found = self._find(predicate, after)
            if found is not None:
                return found
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(changed.wait(), remaining)
            except TimeoutError:
                return self._find(predicate, after)
