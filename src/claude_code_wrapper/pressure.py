"""Context pressure policy: models, level evaluation, text rendering, per-session state.

The orchestrator pushes a `PressurePolicy` per session (memory only; it re-pushes after a
wrapper restart). Levels are computed from the statusLine context gauge in tokens: unknown
context (None, e.g. right after compaction) is level 0, never a stale high level. The
percentage thresholds are used only when the gauge reports no window.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PLACEHOLDER = re.compile(r"\{(pct|used_k|window_k|compact_pct|left_k|level|waiting_on)\}")


class StaleVersion(Exception):
    """A policy older than the one in force was pushed."""

    def __init__(self, pushed: int, current: int) -> None:
        super().__init__(f"policy version {pushed} is older than {current}")
        self.pushed = pushed
        self.current = current


class LevelTexts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full: str
    short: str


class Thresholds(BaseModel):
    l1_tokens: int | None = None
    l2_tokens: int
    l3_tokens: int
    compact_tokens: int
    l1_pct: float | None = None
    l2_pct: float
    l3_pct: float
    compact_pct: float


class PressurePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    thresholds: Thresholds
    profile: Literal["work", "chat"]
    texts: dict[Literal["l1", "l2", "l3"], LevelTexts]
    handoff_state: Literal["none", "submitted", "blocked", "accepted"] = "none"
    waiting_on: list[str] = Field(default_factory=list)
    resume_pending: bool = False
    resume_text: str = ""
    compact_instructions: str = Field(default="", max_length=9000)
    reinject_text: str = Field(default="", max_length=9000)

    def max_text_len(self) -> int:
        """The longest single text field, for the `pressure.max_text_chars` check."""
        lens = [len(self.resume_text), len(self.compact_instructions), len(self.reinject_text)]
        for t in self.texts.values():
            lens += [len(t.full), len(t.short)]
        return max(lens)


def _level(value: float | None, l1: float | None, l2: float, l3: float) -> int:
    if value is None:
        return 0
    if value >= l3:
        return 3
    if value >= l2:
        return 2
    if l1 is not None and value >= l1:
        return 1
    return 0


def evaluate_level(th: Thresholds, context: Mapping[str, Any] | None) -> int:
    """Level 0..3 from the context gauge: tokens first, percent only without a window."""
    if not context:
        return 0
    used = context.get("used_tokens")
    if used is not None:
        return _level(used, th.l1_tokens, th.l2_tokens, th.l3_tokens)
    if context.get("window_tokens") is None:
        return _level(context.get("used_percentage"), th.l1_pct, th.l2_pct, th.l3_pct)
    return 0


def _num(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def render(
    template: str,
    level: int,
    context: Mapping[str, Any],
    th: Thresholds,
    waiting_on: list[str],
) -> str:
    """Fill the placeholders; unknown ones stay literal and missing values render as `?`."""
    used = context.get("used_tokens")
    window = context.get("window_tokens")
    pct = context.get("used_percentage")
    if pct is None and used is not None and window:
        pct = used * 100 / window
    compact_pct = th.compact_tokens * 100 / window if window else th.compact_pct
    values = {
        "pct": "?" if pct is None else _num(pct),
        "used_k": "?" if used is None else str(round(used / 1000)),
        "window_k": "?" if not window else str(round(window / 1000)),
        "compact_pct": _num(compact_pct),
        "left_k": "?" if used is None else str(max(0, round((th.compact_tokens - used) / 1000))),
        "level": str(level),
        "waiting_on": ", ".join(waiting_on) if waiting_on else "?",
    }
    return PLACEHOLDER.sub(lambda m: values[m.group(1)], template)


class PressureState:
    """One session's policy in force plus delivery counters (memory only)."""

    def __init__(self) -> None:
        self.policy: PressurePolicy | None = None
        self.deliveries = 0
        self.stop_blocks_this_turn = 0

    def put(self, policy: PressurePolicy) -> Literal["set", "unchanged"]:
        cur = self.policy
        if cur is not None:
            if policy.version < cur.version:
                raise StaleVersion(policy.version, cur.version)
            if policy == cur:
                return "unchanged"
        self.policy = policy
        return "set"

    def clear(self) -> None:
        self.policy = None
        self.deliveries = 0
        self.stop_blocks_this_turn = 0

    def level(self, context: Mapping[str, Any] | None) -> int:
        if self.policy is None:
            return 0
        return evaluate_level(self.policy.thresholds, context)

    def view(self, context: Mapping[str, Any] | None) -> dict[str, Any]:
        pol = self.policy
        return {
            "version": pol.version if pol else None,
            "level": self.level(context),
            "context": dict(context) if context is not None else None,
            "handoff_state": pol.handoff_state if pol else "none",
            "deliveries": self.deliveries,
            "stop_blocks_this_turn": self.stop_blocks_this_turn,
        }
