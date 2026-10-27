"""Slash command registry and the per-session queue (design D3). Nothing here runs a command."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel

from claude_code_wrapper.config import (
    COMMAND_WILDCARD,
    INSTRUCTIONS_CONTROL,
    MAX_INSTRUCTIONS,
    CommandsConfig,
    CommandsOverride,
)
from claude_code_wrapper.turns import now_iso

#: no leading hyphen, so a server name can never be read as a flag
SERVER_NAME = re.compile(r"[A-Za-z0-9_.:][A-Za-z0-9_.:-]{0,63}")
MAX_QUEUED = 4
MAX_KEPT = 100

#: name -> {param: (json type, description, required)}
BUILTIN_COMMANDS: dict[str, dict[str, tuple[str, str, bool]]] = {
    "compact": {
        "instructions": (
            "string",
            "optional one-line focus instructions for the summary (max 2000 characters)",
            False,
        )
    },
    "reload-plugins": {"force": ("boolean", "apply even when the cache is unchanged", False)},
    "reload-skills": {},
    "mcp": {
        "action": ("string", "must be 'reconnect'", True),
        "server": ("string", "server name; default all servers", False),
    },
    "context": {},
}

Origin = Literal["agent", "client", "idle"]
State = Literal["queued", "running", "completed", "failed", "cancelled"]
_TERMINAL = ("completed", "failed", "cancelled")


class CommandRejected(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def validate_params(name: str, params: Mapping[str, Any] | None, *, allow_force: bool) -> dict:
    spec = BUILTIN_COMMANDS.get(name)
    if spec is None:
        raise CommandRejected(
            "unknown_command",
            f"unknown command {name!r}; allowed: {', '.join(BUILTIN_COMMANDS)}",
        )
    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        raise CommandRejected("invalid_params", "params must be an object")
    unknown = sorted(set(params) - set(spec))
    if unknown:
        allowed = f"allowed: {', '.join(spec)}" if spec else "it takes no params"
        raise CommandRejected(
            "invalid_params", f"/{name}: unknown params {', '.join(unknown)}; {allowed}"
        )
    for key, (kind, _desc, required) in spec.items():
        if key not in params:
            if required:
                raise CommandRejected("invalid_params", f"/{name}: {key} is required")
            continue
        value = params[key]
        if not isinstance(value, str if kind == "string" else bool):
            raise CommandRejected("invalid_params", f"/{name}: {key} must be a {kind}")
    out = dict(params)
    for key, (kind, _desc, _req) in spec.items():
        text = out.get(key)
        if kind != "string" or text is None:
            continue
        if INSTRUCTIONS_CONTROL.search(text):
            raise CommandRejected(
                "invalid_params",
                f"/{name}: {key} must be one line without control characters",
            )
        if len(text) > MAX_INSTRUCTIONS:
            raise CommandRejected(
                "invalid_params", f"/{name}: {key} exceeds {MAX_INSTRUCTIONS} characters"
            )
    if name == "mcp" and "server" in out and not SERVER_NAME.fullmatch(out["server"]):
        raise CommandRejected("invalid_params", "/mcp: server must match ^[A-Za-z0-9_.:-]{1,64}$")
    if name == "mcp" and out["action"] != "reconnect":
        raise CommandRejected("invalid_params", "/mcp: action must be 'reconnect'")
    if name == "reload-plugins" and out.get("force") and not allow_force:
        raise CommandRejected(
            "force_not_allowed", "/reload-plugins --force is not allowed (commands.allow_force)"
        )
    return out


def render(name: str, params: Mapping[str, Any] | None = None) -> str:
    params = params or {}
    if name == "compact":
        text = str(params.get("instructions") or "").strip()
        return f"/compact {text}" if text else "/compact"
    if name == "reload-plugins":
        return "/reload-plugins --force" if params.get("force") else "/reload-plugins"
    if name == "mcp":
        return f"/mcp reconnect {params.get('server') or 'all'}"
    return f"/{name}"


def params_schema(names: list[str] | tuple[str, ...]) -> dict:
    """Flat JSON schema of the MCP tool input `{name, params?}` (no oneOf/anyOf/allOf)."""
    names = [n for n in names if n in BUILTIN_COMMANDS]
    props: dict[str, Any] = {}
    for n in names:
        for key, (kind, desc, required) in BUILTIN_COMMANDS[n].items():
            note = f"/{n}: {desc}" + (" (required)" if required else "")
            if key in props:
                props[key]["description"] += f"; {note}"
            else:
                props[key] = {"type": kind, "description": note}
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string", "enum": names, "description": "the slash command to run"},
            "params": {
                "type": "object",
                "description": "parameters of the chosen command; omit when it takes none",
                "properties": props,
                "additionalProperties": False,
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    }


class CommandRecord(BaseModel):
    id: str
    name: str
    args: dict[str, Any] = {}
    origin: Origin
    turn_id: str | None = None
    state: State = "queued"
    result: str | None = None
    error: str | None = None
    output: str | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None


class CommandQueue:
    def __init__(self, cfg: CommandsConfig) -> None:
        self.cfg = cfg
        self._records: list[CommandRecord] = []

    def request(
        self, name: str, params: Mapping[str, Any] | None, *, origin: Origin, turn_id: str | None
    ) -> tuple[CommandRecord, bool]:
        if origin == "idle":
            allowed = ["compact"]
        else:
            if name.lower() in {d.lower() for d in self.cfg.deny}:
                raise CommandRejected(
                    "denied",
                    f"/{name} is denied (sessions.commands.deny): it would end, replace or move "
                    "the session or change its account or credentials",
                )
            allowed = self.cfg.agent if origin == "agent" else self.cfg.client
        if name not in allowed and COMMAND_WILDCARD not in allowed:
            raise CommandRejected(
                "not_allowed",
                f"/{name} is not allowed for {origin}; allowed: {', '.join(allowed) or 'none'}",
            )
        args = validate_params(name, params, allow_force=self.cfg.allow_force)
        queued = self.pending()
        for rec in queued:
            if rec.name == name:
                return rec, False
        if len(queued) >= MAX_QUEUED:
            raise CommandRejected("queue_full", f"{MAX_QUEUED} commands are already queued")
        rec = CommandRecord(
            id=f"cmd_{uuid.uuid4().hex[:12]}",
            name=name,
            args=args,
            origin=origin,
            turn_id=turn_id,
            created_at=now_iso(),
        )
        self._records.append(rec)
        del self._records[:-MAX_KEPT]
        return rec, True

    def pending(self) -> list[CommandRecord]:
        return [r for r in self._records if r.state == "queued"]

    def next(self) -> CommandRecord | None:
        pending = self.pending()
        return pending[0] if pending else None

    def get(self, command_id: str) -> CommandRecord | None:
        return next((r for r in self._records if r.id == command_id), None)

    def list(self) -> list[CommandRecord]:
        return list(self._records)

    def start(self, record: CommandRecord) -> None:
        if record.state == "queued":
            record.state = "running"
            record.started_at = now_iso()

    def finish(
        self,
        record: CommandRecord,
        state: Literal["completed", "failed", "cancelled"],
        result: str | None = None,
        error: str | None = None,
        output: str | None = None,
    ) -> None:
        if record.state in _TERMINAL:
            return
        record.state = state
        record.result = result
        record.error = error
        record.output = output
        record.finished_at = now_iso()

    def cancel_queued(self, turn_id: str | None = None) -> list[CommandRecord]:
        out = []
        for rec in self.pending():
            if turn_id is None or rec.turn_id == turn_id:
                self.finish(rec, "cancelled")
                out.append(rec)
        return out


def effective_commands(base: CommandsConfig, override: CommandsOverride | None) -> CommandsConfig:
    if override is None:
        return base
    return base.model_copy(update=override.model_dump(exclude_none=True))
