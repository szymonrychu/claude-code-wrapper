"""Prometheus metrics (D18): one `Metrics` per service, on its own registry."""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client import generate_latest as _generate_latest

from .config import BUILTIN_COMMAND_NAMES
from .turns import TurnRecord

MAX_TOOL_LABELS = 50
OTHER_TOOL = "other"
PEER_KINDS = ("message", "notice")
PEER_OPS = ("join", "heartbeat", "leave", "inbound_heartbeat")
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

_TOKEN_KINDS = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_read_input_tokens": "cache_read",
    "cache_creation_input_tokens": "cache_creation",
}


def _seconds_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        delta = datetime.datetime.fromisoformat(end) - datetime.datetime.fromisoformat(start)
    except ValueError:
        return None
    return max(delta.total_seconds(), 0.0)


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry if registry is not None else CollectorRegistry()
        reg = self.registry
        self._sessions_active = Gauge(
            "ccw_sessions_active", "Sessions currently held.", registry=reg
        )
        self._sessions_created = Counter(
            "ccw_sessions_created_total", "Session create attempts.", ["result"], registry=reg
        )
        self._turns = Counter(
            "ccw_turns_total",
            "Turns that reached a terminal state.",
            ["origin", "state"],
            registry=reg,
        )
        self._turn_duration = Histogram("ccw_turn_duration_seconds", "Turn duration.", registry=reg)
        self._tool_calls = Counter(
            "ccw_tool_calls_total", "Tool calls.", ["tool", "decision"], registry=reg
        )
        self._hook_actions = Counter(
            "ccw_hook_actions_total", "Hook actions run.", ["event", "type", "result"], registry=reg
        )
        self._restarts = Counter("ccw_claude_restarts_total", "Claude restarts.", registry=reg)
        self._tokens = Counter(
            "ccw_turn_tokens_total", "Tokens used by turns.", ["model", "kind"], registry=reg
        )
        self._http_requests = Counter(
            "ccw_http_requests_total",
            "Public API responses.",
            ["route", "method", "status_code"],
            registry=reg,
        )
        self._http_panics = Counter(
            "ccw_http_panics_total", "Unhandled exceptions in handlers.", registry=reg
        )
        self._probe_outcomes = Counter(
            "ccw_probe_outcomes_total", "Probe outcomes.", ["outcome"], registry=reg
        )
        self._probe_answer = Histogram(
            "ccw_probe_answer_seconds", "Probe created to answered.", registry=reg
        )
        self._stalls = Counter("ccw_stall_suspected_total", "Stall reports.", registry=reg)
        self._interrupts = Counter(
            "ccw_interrupts_total", "Interrupts by result.", ["result"], registry=reg
        )
        self._commands = Counter(
            "ccw_commands_total",
            "Slash commands that reached a final state.",
            ["command", "origin", "result"],
            registry=reg,
        )
        self._command_duration = Histogram(
            "ccw_command_duration_seconds", "Command duration.", ["command"], registry=reg
        )
        self._compactions = Counter(
            "ccw_compactions_total", "Compactions by trigger.", ["trigger"], registry=reg
        )
        self._peer_received = Counter(
            "ccw_peer_messages_received_total",
            "Inbound peer messages and notices by result.",
            ["kind", "result"],
            registry=reg,
        )
        self._peer_sent = Counter(
            "ccw_peer_messages_sent_total",
            "Outbound peer messages and notices by result.",
            ["kind", "result"],
            registry=reg,
        )
        self._peer_delivered = Counter(
            "ccw_peer_messages_delivered_total",
            "Peer messages and notices by inbox delivery result.",
            ["kind", "result"],
            registry=reg,
        )
        self._peer_membership = Counter(
            "ccw_peer_membership_total",
            "Peer membership rounds by op and result.",
            ["op", "result"],
            registry=reg,
        )
        self._peer_members = Gauge(
            "ccw_peer_members", "Peers currently in the reachable list.", registry=reg
        )
        self._tools_seen: set[str] = set()

    # ------------------------------------------------------------------ sessions

    def session_opened(self) -> None:
        self._sessions_active.inc()

    def session_closed(self) -> None:
        self._sessions_active.dec()

    def sessions_created(self, result: str) -> None:
        self._sessions_created.labels(result).inc()

    # ------------------------------------------------------------------ turns

    def turn_finished(self, record: TurnRecord) -> None:
        self._turns.labels(record.origin, record.state).inc()
        seconds = _seconds_between(record.started_at, record.completed_at)
        if seconds is not None:
            self._turn_duration.observe(seconds)

    def tokens(self, per_model: Mapping[str, Mapping[str, Any]] | None) -> None:
        for model, bucket in (per_model or {}).items():
            for field, kind in _TOKEN_KINDS.items():
                count = bucket.get(field) or 0
                if count > 0:
                    self._tokens.labels(model, kind).inc(count)

    def tool_call(self, tool: str, decision: str) -> None:
        """The first `MAX_TOOL_LABELS` distinct names keep their label, later ones are `other`."""
        if tool not in self._tools_seen:
            if len(self._tools_seen) < MAX_TOOL_LABELS:
                self._tools_seen.add(tool)
            else:
                tool = OTHER_TOOL
        self._tool_calls.labels(tool, decision).inc()

    def hook_action(self, event: str, type: str, ok: bool) -> None:
        self._hook_actions.labels(event, type, "ok" if ok else "error").inc()

    def claude_restart(self) -> None:
        self._restarts.inc()

    # ------------------------------------------------------------------ http, probes, stalls

    def http_request(self, route: str, method: str, status_code: int) -> None:
        self._http_requests.labels(route, method, str(status_code)).inc()

    def http_panic(self) -> None:
        self._http_panics.inc()

    def probe_outcome(self, outcome: str, seconds: float | None = None) -> None:
        """`sent`, `rejected`, `delivered` or `answered` (with the created-to-answered seconds)."""
        self._probe_outcomes.labels(outcome).inc()
        if seconds is not None:
            self._probe_answer.observe(seconds)

    def probe_answered(self, probe: Any) -> None:
        self.probe_outcome("answered", _seconds_between(probe.created_at, probe.answered_at))

    def stall_suspected(self) -> None:
        self._stalls.inc()

    def interrupt(self, result: str) -> None:
        """`interrupted`, `timeout` or `idle` (no running turn)."""
        self._interrupts.labels(result).inc()

    def command_finished(self, command: str, origin: str, result: str, seconds: float) -> None:
        """`result` is the final state: completed, failed or cancelled."""
        name = command if command in BUILTIN_COMMAND_NAMES else "other"
        self._commands.labels(name, origin, result).inc()
        self._command_duration.labels(name).observe(max(0.0, seconds))

    def compaction(self, trigger: str) -> None:
        """`manual`, `auto` or `unknown` (anything else counts as unknown)."""
        self._compactions.labels(trigger if trigger in ("manual", "auto") else "unknown").inc()

    # ------------------------------------------------------------------ peers

    @staticmethod
    def _peer_kind(kind: str) -> str:
        if kind not in PEER_KINDS:
            raise ValueError(f"unknown peer kind {kind!r}")
        return kind

    def peer_received(self, kind: str, result: str) -> None:
        """`kind` is `message` or `notice`; `result` is the endpoint outcome."""
        self._peer_received.labels(self._peer_kind(kind), result).inc()

    def peer_sent(self, kind: str, result: str) -> None:
        """`result` is `ok` or the PeerError code."""
        self._peer_sent.labels(self._peer_kind(kind), result).inc()

    def peer_delivered(self, kind: str, result: str) -> None:
        """`result` is `delivered`, `rejected` or `dropped`."""
        self._peer_delivered.labels(self._peer_kind(kind), result).inc()

    def peer_membership(self, op: str, result: str) -> None:
        """`op` is `join`, `heartbeat`, `leave` or `inbound_heartbeat`; `result` ok/failed/vetoed."""
        if op not in PEER_OPS:
            raise ValueError(f"unknown peer membership op {op!r}")
        self._peer_membership.labels(op, result).inc()

    def peer_members(self, count: int) -> None:
        self._peer_members.set(count)

    # ------------------------------------------------------------------ exposition

    def render(self) -> bytes:
        return _generate_latest(self.registry)
