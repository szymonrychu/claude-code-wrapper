"""Broker for relayed PermissionRequest hooks: one single-assignment future per request.

A held hook awaits `PendingPermission.future`; whoever resolves it first (a human answer,
the hold deadline, a dropped hook connection, the terminal) wins and later resolves are
refused. `resolve` is synchronous, so the check and the assignment cannot interleave.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from claude_code_wrapper.redact import Redactor
from claude_code_wrapper.timeutil import iso

Behavior = Literal["allow", "deny"]
ResolvedBy = Literal["user", "timeout", "gone", "terminal"]
TOOL_INPUT_MAX_BYTES = 4096
HOLD_DENY_MESSAGE = (
    "No operator answered the permission request in time; continue without this action"
)
RESOLVED_MEMORY = 256


@dataclass(frozen=True)
class Decision:
    behavior: Behavior
    by: ResolvedBy
    message: str | None = None


@dataclass
class PendingPermission:
    request_id: str
    tool_name: str
    tool_input: Any  # raw, for PostToolUse matching only, never serialised
    agent_id: str | None
    view_input: str
    truncated: bool
    suggestions: list[Any]
    since: str
    expires_at: str
    future: asyncio.Future[Decision] = field(repr=False)

    def view(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "tool_name": self.tool_name,
            "tool_input": self.view_input,
            "truncated": self.truncated,
            "suggestions": self.suggestions,
            "agent_id": self.agent_id,
            "since": self.since,
            "expires_at": self.expires_at,
        }


class PermissionBroker:
    def __init__(self, redactor: Redactor) -> None:
        self._redactor = redactor
        self._pending: OrderedDict[str, PendingPermission] = OrderedDict()
        self._resolved: OrderedDict[str, None] = OrderedDict()

    def open(self, payload: Mapping[str, Any], hold_seconds: float) -> PendingPermission:
        tool_input = payload.get("tool_input")
        view_input, truncated = self._view_input(tool_input)
        suggestions = payload.get("permission_suggestions")
        agent_id = payload.get("agent_id")
        since = datetime.datetime.now(datetime.UTC)
        pending = PendingPermission(
            request_id="perm_" + uuid.uuid4().hex[:12],
            tool_name=str(payload.get("tool_name") or ""),
            tool_input=tool_input,
            agent_id=agent_id if isinstance(agent_id, str) else None,
            view_input=view_input,
            truncated=truncated,
            suggestions=self._redactor.redact_obj(suggestions)
            if isinstance(suggestions, list)
            else [],
            since=iso(since),
            expires_at=iso(since + datetime.timedelta(seconds=hold_seconds)),
            future=asyncio.get_running_loop().create_future(),
        )
        self._pending[pending.request_id] = pending
        return pending

    def _view_input(self, tool_input: Any) -> tuple[str, bool]:
        """Redacted JSON of the input cut to TOOL_INPUT_MAX_BYTES on a UTF-8 boundary."""
        text = json.dumps(
            self._redactor.redact_obj(tool_input), indent=2, ensure_ascii=False, default=str
        )
        raw = self._redactor.redact(text).encode("utf-8")
        if len(raw) <= TOOL_INPUT_MAX_BYTES:
            return raw.decode("utf-8"), False
        return raw[:TOOL_INPUT_MAX_BYTES].decode("utf-8", errors="ignore"), True

    def resolve(self, request_id: str, decision: Decision) -> bool:
        pending = self._pending.pop(request_id, None)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(decision)
        self._resolved[request_id] = None
        while len(self._resolved) > RESOLVED_MEMORY:
            self._resolved.popitem(last=False)
        return True

    def first(self) -> PendingPermission | None:
        return next(iter(self._pending.values()), None)

    def get(self, request_id: str) -> PendingPermission | None:
        return self._pending.get(request_id)

    def was_resolved(self, request_id: str) -> bool:
        return request_id in self._resolved

    def match_tool(
        self, tool_name: str, tool_input: Any, agent_id: str | None
    ) -> PendingPermission | None:
        for p in self._pending.values():
            if p.tool_name == tool_name and p.agent_id == agent_id and p.tool_input == tool_input:
                return p
        return None

    def of_agent(self, agent_id: str | None) -> list[PendingPermission]:
        return [p for p in self._pending.values() if p.agent_id == agent_id]

    def resolve_all(self, decision: Decision) -> list[str]:
        ids = list(self._pending)
        for request_id in ids:
            self.resolve(request_id, decision)
        return ids

    def __len__(self) -> int:
        return len(self._pending)
