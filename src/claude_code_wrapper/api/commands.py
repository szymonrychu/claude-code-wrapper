"""Slash command routes: queue, list and read allowlisted commands on the session's queue."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from claude_code_wrapper.api.errors import ApiError
from claude_code_wrapper.api.redaction import _out
from claude_code_wrapper.commands import CommandRejected
from claude_code_wrapper.manager import InvalidState

router = APIRouter()

#: CommandRejected.code -> (status, envelope code)
_REJECT = {
    "not_allowed": (403, "command_not_allowed"),
    "unknown_command": (422, "invalid_command"),
    "invalid_params": (422, "invalid_command"),
    "force_not_allowed": (422, "invalid_command"),
    "queue_full": (429, "command_queue_full"),
}


class CommandRequest(BaseModel):
    """`POST /v1/sessions/{id}/commands` body."""

    model_config = ConfigDict(extra="forbid")

    name: str
    args: dict[str, Any] = {}


def _live(request: Request, session_id: str):
    try:
        return request.app.state.manager.router(session_id)
    except InvalidState as exc:
        raise ApiError(409, "dead", "the session is not running") from exc


def _dump(request: Request, record: Any) -> Any:
    return _out(request, record.model_dump(mode="json"))


@router.post("/sessions/{session_id}/commands", status_code=202)
async def queue_command(session_id: str, body: CommandRequest, request: Request) -> Any:
    live = _live(request, session_id)
    try:
        record = live.request_command(body.name, body.args, origin="client")
    except CommandRejected as exc:
        if exc.code == "invalid_state":
            code = "booting" if exc.message.endswith("booting") else "dead"
            raise ApiError(409, code, exc.message) from exc
        status, code = _REJECT.get(exc.code, (422, "invalid_command"))
        raise ApiError(status, code, exc.message) from exc
    return _dump(request, record)


@router.get("/sessions/{session_id}/commands")
async def list_commands(session_id: str, request: Request) -> Any:
    live = _live(request, session_id)
    return _out(request, {"commands": [r.model_dump(mode="json") for r in live.commands.list()]})


@router.get("/sessions/{session_id}/commands/{command_id}")
async def get_command(session_id: str, command_id: str, request: Request) -> Any:
    record = _live(request, session_id).commands.get(command_id)
    if record is None:
        raise ApiError(404, "command_not_found", f"unknown command {command_id!r}")
    return _dump(request, record)
