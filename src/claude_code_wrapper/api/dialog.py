"""Dialog routes: what a session waits on, and the person's answer (allow, deny, dismiss)."""

from __future__ import annotations

from typing import Any, Literal, Self

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from claude_code_wrapper.api.errors import ApiError
from claude_code_wrapper.api.turns import _live_router
from claude_code_wrapper.router import DialogConflict

router = APIRouter()

REQUEST_ID_PATTERN = r"^perm_[0-9a-f]{12}$"


class DialogAction(BaseModel):
    """`POST /v1/sessions/{id}/dialog` body: `allow` and `deny` name the held request."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["allow", "deny", "dismiss"]
    request_id: str | None = Field(default=None, pattern=REQUEST_ID_PATTERN)
    expect_since: str | None = None
    message: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _allow_deny_need_request_id(self) -> Self:
        if self.action != "dismiss" and self.request_id is None:
            raise ValueError("request_id is required for allow and deny")
        return self


@router.get("/sessions/{session_id}/dialog")
async def get_dialog(session_id: str, request: Request) -> dict[str, Any]:
    return _live_router(request, session_id).dialog_view()


@router.post("/sessions/{session_id}/dialog")
async def post_dialog(session_id: str, body: DialogAction, request: Request) -> dict[str, Any]:
    live = _live_router(request, session_id)
    try:
        return await live.answer_dialog(
            body.action, body.request_id, body.expect_since, body.message
        )
    except DialogConflict as exc:
        raise ApiError(409, exc.code, str(exc)) from exc
