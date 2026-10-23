"""Session file routes: read-only listing and preview of the session workdir (D1)."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request

from claude_code_wrapper import files
from claude_code_wrapper.api.errors import ApiError

router = APIRouter()


def _workdir(request: Request, session_id: str) -> str | None:
    """The session's workdir; `SessionNotFound` (404) for an unknown session."""
    workdir = request.app.state.manager.get(session_id).get("workdir")
    return str(workdir) if workdir else None


def _raise(exc: files.FileError) -> ApiError:
    return ApiError(404 if exc.code == "not_found" else 400, exc.code, exc.message)


@router.get("/sessions/{session_id}/files")
async def list_files(
    session_id: str,
    request: Request,
    path: Annotated[str, Query()] = "",
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    workdir = _workdir(request, session_id)
    limit = request.app.state.cfg.limits.files_max_entries
    try:
        page = await asyncio.to_thread(files.list_directory, workdir, path, offset, limit)
    except files.FileError as exc:
        raise _raise(exc) from exc
    return {"session_id": session_id, **page}


@router.get("/sessions/{session_id}/files/content")
async def file_content(
    session_id: str, request: Request, path: Annotated[str, Query()] = ""
) -> Any:
    workdir = _workdir(request, session_id)
    cap = request.app.state.cfg.limits.file_preview_max_bytes
    try:
        body = await asyncio.to_thread(files.read_preview, workdir, path, cap)
    except files.FileError as exc:
        raise _raise(exc) from exc
    return {"session_id": session_id, **body}
