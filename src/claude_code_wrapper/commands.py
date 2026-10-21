"""Slash command registry and the per-session queue (design D3). Nothing here runs a command."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel

from claude_code_wrapper.config import BUILTIN_COMMAND_NAMES, CommandsConfig, CommandsOverride
from claude_code_wrapper.turns import now_iso

MAX_QUEUED = 4
MAX_KEPT = 100
MAX_INSTRUCTIONS = 2000
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")

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
assert tuple(sorted(BUILTIN_COMMANDS)) == tuple(sorted(BUILTIN_COMMAND_NAMES))

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
            f"unknown command {name!r}; allowed: {', '.join(BUILTIN_COMMAND_NAMES)}",
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
    if name == "compact" and "instructions" in out:
        text = out["instructions"]
        if _CONTROL.search(text):
            raise CommandRejected(
                "invalid_params",
                "/compact: instructions must be one line without control characters",
            )
        if len(text) > MAX_INSTRUCTIONS:
            raise CommandRejected(
                "invalid_params", f"/compact: instructions exceed {MAX_INSTRUCTIONS} characters"
            )
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
    """JSON schema of the MCP tool input `{name, params?}` for the given command names."""
    names = [n for n in names if n in BUILTIN_COMMANDS]
    variants = []
    for n in names:
        spec = BUILTIN_COMMANDS[n]
        props = {k: {"type": t, "description": d} for k, (t, d, _r) in spec.items()}
        required = [k for k, (_t, _d, r) in spec.items() if r]
        params: dict[str, Any] = {
            "type": "object",
            "properties": props,
            "additionalProperties": False,
        }
        if required:
            params["required"] = required
        variants.append(
            {
                "type": "object",
                "properties": {"name": {"const": n}, "params": params},
                "required": ["name", *(["params"] if required else [])],
                "additionalProperties": False,
                "description": f"/{n}" + ("" if props else " (takes no params)"),
            }
        )
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string", "enum": names, "description": "the slash command to run"},
            "params": {
                "type": "object",
                "description": "; ".join(
                    f"{n}: "
                    + (
                        ", ".join(f"{k} ({d})" for k, (_t, d, _r) in BUILTIN_COMMANDS[n].items())
                        or "no params"
                    )
                    for n in names
                ),
            },
        },
        "required": ["name"],
        "additionalProperties": False,
        "oneOf": variants,
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
        elif origin == "agent":
            allowed = self.cfg.agent
        else:
            allowed = self.cfg.client
        if name not in allowed:
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
