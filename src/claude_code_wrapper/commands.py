"""Slash command validation and the per-session queue (design D3, D4). Nothing here runs one."""

from __future__ import annotations

import re
import uuid
from typing import Literal

from pydantic import BaseModel

from claude_code_wrapper.config import (
    COMMAND_NAME,
    COMMAND_WILDCARD,
    CommandsConfig,
    CommandsOverride,
)
from claude_code_wrapper.turns import now_iso

MAX_QUEUED = 4
MAX_KEPT = 100
#: Characters of a command's args (D2): webhook prompt templates render issue bodies.
MAX_COMMAND_ARGS = 20000
#: Control characters args may not hold: all but newline and tab, so args cannot end the
#: bracketed paste they are typed in (ESC) or submit early (CR); plus the Unicode line breaks.
ARGS_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]")

Origin = Literal["agent", "client", "idle"]
State = Literal["queued", "running", "completed", "failed", "cancelled"]
_TERMINAL = ("completed", "failed", "cancelled")


class CommandRejected(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def validate(name: str, args: str, *, allow_force: bool) -> str:
    """The normalised, stripped args of `/name args`; `CommandRejected` when it may not run."""
    if not name:
        raise CommandRejected(
            "invalid_command", "the command name is empty; commands are in the form /command [args]"
        )
    if not COMMAND_NAME.fullmatch(name):
        raise CommandRejected(
            "invalid_command", f"invalid command name {name!r}; must match {COMMAND_NAME.pattern}"
        )
    text = args.replace("\r\n", "\n")
    if ARGS_CONTROL.search(text):
        raise CommandRejected(
            "invalid_command",
            f"/{name}: args must not hold control characters other than newline and tab",
        )
    text = text.strip()
    if len(text) > MAX_COMMAND_ARGS:
        raise CommandRejected(
            "invalid_command", f"/{name}: args exceed {MAX_COMMAND_ARGS} characters"
        )
    if name.lower() == "reload-plugins" and "--force" in text.split() and not allow_force:
        raise CommandRejected(
            "force_not_allowed", "/reload-plugins --force is not allowed (commands.allow_force)"
        )
    return text


def render(name: str, args: str = "") -> str:
    return f"/{name} {args}".rstrip()


class CommandRecord(BaseModel):
    id: str
    name: str
    args: str = ""
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
        self, name: str, args: str, *, origin: Origin, turn_id: str | None
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
        args = validate(name, args, allow_force=self.cfg.allow_force)
        queued = self.pending()
        for rec in queued:
            if (rec.name, rec.args) == (name, args):
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
    """The session's commands config: the override's set fields replace the base's, except `deny`.

    A per-session `deny` only extends the base list (case-insensitively, base order kept, the
    override's new names after it); it can never shorten or empty it.
    """
    if override is None:
        return base
    update = override.model_dump(exclude_none=True)
    if "deny" in update:
        deny = list(base.deny)
        seen = {name.lower() for name in deny}
        for name in update["deny"]:
            if name.lower() not in seen:
                seen.add(name.lower())
                deny.append(name)
        update["deny"] = deny
    return base.model_copy(update=update)
