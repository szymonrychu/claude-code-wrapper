"""Session file routes: listing, preview and download of the session workdir (D1) and the write
of an existing file, `PUT .../files/content`."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.background import BackgroundTask

from claude_code_wrapper import files
from claude_code_wrapper.api.errors import ApiError

router = APIRouter()


def _workdir(request: Request, session_id: str) -> str | None:
    """The session's workdir; `SessionNotFound` (404) for an unknown session."""
    workdir = request.app.state.manager.get(session_id).get("workdir")
    return str(workdir) if workdir else None


_STATUS = {"not_found": 404, "file_changed": 409, "too_large": 413}


def _raise(exc: files.FileError) -> ApiError:
    return ApiError(_STATUS.get(exc.code, 400), exc.code, exc.message)


class FileStamp(BaseModel):
    """The `mtime` and `size` of a file as the `files/content` route reported them."""

    model_config = ConfigDict(extra="forbid")

    mtime: str
    size: int = Field(ge=0)


class WriteFile(BaseModel):
    """Body of a file write: both keys are required; `expected: null` overwrites unconditionally."""

    model_config = ConfigDict(extra="forbid")

    content: str
    expected: FileStamp | None

    @field_validator("content")
    @classmethod
    def _utf8(cls, value: str) -> str:
        value.encode("utf-8")  # a lone surrogate is not text a file can hold
        return value


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


@router.put("/sessions/{session_id}/files/content")
async def write_file_content(
    session_id: str, request: Request, body: WriteFile, path: Annotated[str, Query()] = ""
) -> Any:
    workdir = _workdir(request, session_id)
    cap = request.app.state.cfg.limits.file_preview_max_bytes
    expected = body.expected.model_dump() if body.expected else None
    try:
        written = await asyncio.to_thread(
            files.write_file, workdir, path, body.content, expected, cap
        )
    except files.FileError as exc:
        raise _raise(exc) from exc
    return {"session_id": session_id, **written}


async def _stream(handle: files.Download) -> AsyncIterator[bytes]:
    """The file's bytes in chunks read off the event loop; the descriptor is closed when the
    stream ends, fails or is cancelled."""
    try:
        while chunk := await asyncio.to_thread(handle.read_chunk):
            yield chunk
    finally:
        handle.close()


@router.get("/sessions/{session_id}/files/download")
async def file_download(
    session_id: str, request: Request, path: Annotated[str, Query()] = ""
) -> StreamingResponse:
    workdir = _workdir(request, session_id)
    try:
        handle = await asyncio.to_thread(files.open_download, workdir, path)
    except files.FileError as exc:
        raise _raise(exc) from exc
    return StreamingResponse(
        _stream(handle),
        headers={
            "Content-Type": handle.mime or "application/octet-stream",
            "Content-Length": str(handle.size),
            "Content-Disposition": files.content_disposition(handle.name),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
        background=BackgroundTask(handle.close),
    )
