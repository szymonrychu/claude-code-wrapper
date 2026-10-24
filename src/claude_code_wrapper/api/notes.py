"""`POST /v1/sessions/{id}/notes`: push a note into a hived session's inbox."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from claude_code_wrapper.api.errors import ApiError

router = APIRouter()

NoteKey = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,40}$")]


class NoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    key: NoteKey | None = None
    source: str | None = Field(default=None, max_length=80)


@router.post("/sessions/{session_id}/notes", status_code=202)
async def push_note(session_id: str, body: NoteRequest, request: Request) -> Any:
    cfg = request.app.state.cfg.notes
    if not cfg.enabled:
        raise ApiError(409, "notes_disabled", "notes are disabled (notes.enabled is false)")
    if len(body.text) > cfg.max_note_chars:
        raise ApiError(
            422, "invalid_request", f"text: must be at most {cfg.max_note_chars} characters"
        )
    manager = request.app.state.manager
    store = manager.note_store(session_id)
    log = manager.open_event_log(session_id)
    store.put(body.text, key=body.key, source=body.source)
    log.append(
        "note_received",
        {"key": body.key, "source": body.source, "text": body.text, "chars": len(body.text)},
    )
    return {"key": body.key, "pending": store.pending()}
