"""Runaway guard: pure counters for Stop-hook continuations, subagent spawns and repeated
identical failing tool calls. No I/O; `SessionRouter` owns one and emits `guard_tripped`."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .config import GuardConfig

SUBAGENT_TOOLS: frozenset[str] = frozenset({"Agent", "Task"})
STOP_LOOP_REASON = (
    "The runaway guard ended this turn: Stop hooks continued it {limit} times. "
    "Summarise where you are and wait for a new message."
)
AGENT_SPAWN_DENY_REASON = (
    "The runaway guard refused this subagent: this session already launched {limit} subagents. "
    "Finish the work with the subagents you have or in the main conversation."
)
BREAKER_DENY_REASON = (
    "This exact call failed {count} times in a row; change the input or the approach."
)

#: (agent_id or "main", tool_name, sha256 hex of the canonical tool input)
CallKey = tuple[str, str, str]

_DENIED_IDS_KEPT = 256


def call_key(agent_id: str | None, tool_name: str, tool_input: Any) -> CallKey:
    raw = json.dumps(tool_input, sort_keys=True, separators=(",", ":"), default=str)
    return (agent_id or "main", tool_name, hashlib.sha256(raw.encode()).hexdigest())


@dataclass(frozen=True)
class SpawnVerdict:
    allowed: bool
    count: int
    first_denial: bool


@dataclass(frozen=True)
class BreakerVerdict:
    deny: bool
    streak: int
    first_trip: bool


class _Breaker:
    __slots__ = ("last_key", "streak", "reported")

    def __init__(self) -> None:
        self.last_key: CallKey | None = None
        self.streak = 0
        self.reported = False


class RunawayGuard:
    def __init__(self, cfg: GuardConfig) -> None:
        self.cfg = cfg
        self._turn_id: str | None = None
        self._continuations = 0
        self._loop_tripped = False
        self._spawns: dict[str, int] = {}
        self._spawn_denied: set[str] = set()
        self._breakers: dict[str, _Breaker] = {}
        self._denied_ids: OrderedDict[str, None] = OrderedDict()

    # Stop-hook continuations

    def stop_continuation(self, turn_id: str, stop_hook_active: bool) -> int | None:
        limit = self.cfg.max_stop_continuations
        if limit == 0:
            return None
        if turn_id != self._turn_id:
            self._turn_id = turn_id
            self._continuations = 0
        if stop_hook_active and self._continuations < limit:
            self._continuations += 1
        if self._continuations >= limit:
            self._loop_tripped = True
            return self._continuations
        return None

    @property
    def loop_tripped(self) -> bool:
        return self._loop_tripped

    def clear_loop_trip(self) -> None:
        self._loop_tripped = False

    # Subagent spawns

    def spawn(self, claude_session_id: str) -> SpawnVerdict:
        limit = self.cfg.max_agent_spawns
        if limit == 0:
            return SpawnVerdict(True, 0, False)
        count = self._spawns.get(claude_session_id, 0)
        if count >= limit:
            first = claude_session_id not in self._spawn_denied
            self._spawn_denied.add(claude_session_id)
            return SpawnVerdict(False, count, first)
        self._spawns[claude_session_id] = count + 1
        return SpawnVerdict(True, count + 1, False)

    # Repeated identical failing calls

    def _breaker(self, agent: str) -> _Breaker:
        return self._breakers.setdefault(agent, _Breaker())

    def breaker_check(self, key: CallKey) -> BreakerVerdict:
        limit = self.cfg.tool_failure_repeats
        b = self._breakers.get(key[0])
        if limit == 0 or b is None or b.last_key != key or b.streak < limit:
            return BreakerVerdict(False, b.streak if b is not None else 0, False)
        first = not b.reported
        b.reported = True
        return BreakerVerdict(True, b.streak, first)

    def _apply(self, key: CallKey, failed: bool) -> None:
        b = self._breaker(key[0])
        if not failed:
            b.streak = 0
            b.reported = False
            b.last_key = key
            return
        if b.last_key == key:
            b.streak += 1
        else:
            b.last_key = key
            b.streak = 1
            b.reported = False

    def record_result(self, key: CallKey, *, failed: bool, tool_use_id: str | None) -> None:
        if tool_use_id is not None and tool_use_id in self._denied_ids:
            del self._denied_ids[tool_use_id]
            return
        self._apply(key, failed)

    def record_denial(self, key: CallKey, tool_use_id: str | None) -> None:
        self._apply(key, True)
        if tool_use_id is not None:
            self._denied_ids[tool_use_id] = None
            while len(self._denied_ids) > _DENIED_IDS_KEPT:
                self._denied_ids.popitem(last=False)

    def streak(self, agent: str) -> int:
        b = self._breakers.get(agent)
        return b.streak if b is not None else 0
