"""Session routes: thin handlers over the manager."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict

from claude_code_wrapper.api.errors import ApiError
from claude_code_wrapper.api.redaction import _out
from claude_code_wrapper.claude_env import ClaudeEnv
from claude_code_wrapper.manager import CreateSessionRequest
from claude_code_wrapper.prompts import list_templates

router = APIRouter()

SessionState = Literal["creating", "ready", "busy", "stopped", "failed", "dead"]


class ResumeRequest(BaseModel):
    """`POST /v1/sessions/{id}/resume` body: secret env overrides are resent here."""

    model_config = ConfigDict(extra="forbid")

    env: ClaudeEnv | None = None  # type: ignore[valid-type]


@router.post("/sessions", status_code=201)
async def create_session(body: CreateSessionRequest, request: Request) -> Any:
    url = body.callback_url
    if url is not None:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ApiError(400, "invalid_callback_url", "callbackUrl must be an http or https URL")
    return _out(request, await request.app.state.manager.create(body))


@router.get("/sessions")
async def list_sessions(
    request: Request, state: Annotated[SessionState | None, Query()] = None
) -> Any:
    return _out(request, {"sessions": request.app.state.manager.list(state)})


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, request: Request) -> Any:
    return _out(request, request.app.state.manager.get(session_id))


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, request: Request, purge: bool = False) -> Any:
    return _out(request, await request.app.state.manager.destroy(session_id, purge=purge))


@router.post("/sessions/{session_id}/resume")
async def resume_session(
    session_id: str, request: Request, body: ResumeRequest | None = None
) -> Any:
    env = body.env if body is not None else None
    return _out(request, await request.app.state.manager.resume(session_id, env))


@router.get("/config")
async def get_config(request: Request) -> Any:
    return _out(request, request.app.state.cfg.redacted())


@router.get("/prompts")
async def get_prompts(request: Request) -> Any:
    return {"templates": list_templates(request.app.state.cfg)}
