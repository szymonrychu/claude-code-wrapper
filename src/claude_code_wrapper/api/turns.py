"""Turn, interrupt, probe and transcript routes: thin handlers over the session router."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from claude_code_wrapper.api.errors import ApiError
from claude_code_wrapper.api.redaction import _out
from claude_code_wrapper.manager import InvalidState
from claude_code_wrapper.prompts import resolve_prompt
from claude_code_wrapper.router import SessionRouter, TurnVetoed
from claude_code_wrapper.session import TurnRejected
from claude_code_wrapper.turns import METADATA_MAX_BYTES
from claude_code_wrapper.workdir import FileWriteError

router = APIRouter()

_REJECT_STATUS = {"busy": 409, "booting": 503, "dead": 503, "slash_command": 422}
_REJECT_TEXT = {
    "busy": "a turn is already running",
    "booting": "the session is still booting",
    "dead": "the session is not running",
    "slash_command": "slash commands go to POST /v1/sessions/{id}/commands",
}


class TurnRequest(BaseModel):
    """`POST /v1/sessions/{id}/turns` body: exactly one of `text` and `template`."""

    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    template: str | None = None
    variables: dict[str, Any] | None = None
    files: dict[str, str] | None = None
    callback_url: str | None = Field(default=None, alias="callbackUrl")
    metadata: dict[str, Any] | None = None


class ProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = None


def _live_router(request: Request, session_id: str) -> SessionRouter:
    """The running session's router; a stopped or dead session is 503 `dead`."""
    try:
        return request.app.state.manager.router(session_id)
    except InvalidState as exc:
        raise ApiError(503, "dead", "the session is not running") from exc


def _rejected(exc: TurnRejected) -> ApiError:
    return ApiError(
        _REJECT_STATUS.get(exc.reason, 503),
        exc.reason,
        _REJECT_TEXT.get(exc.reason, "the session cannot take input now"),
    )


@router.post("/sessions/{session_id}/turns", status_code=202)
async def submit_turn(session_id: str, body: TurnRequest, request: Request) -> Any:
    live = _live_router(request, session_id)
    cfg = request.app.state.cfg
    text = resolve_prompt(cfg, text=body.text, template=body.template, variables=body.variables)
    assert text is not None
    if len(text.encode("utf-8")) > cfg.limits.max_prompt_bytes:
        raise ApiError(413, "too_large", f"prompt exceeds {cfg.limits.max_prompt_bytes} bytes")
    metadata = body.metadata or {}
    if len(json.dumps(metadata, ensure_ascii=False).encode("utf-8")) > METADATA_MAX_BYTES:
        raise ApiError(413, "too_large", f"metadata exceeds {METADATA_MAX_BYTES} bytes")
    url = body.callback_url
    if url is not None:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ApiError(400, "invalid_callback_url", "callbackUrl must be an http or https URL")
    try:
        record = await live.submit_turn(text, files=body.files, callback_url=url, metadata=metadata)
    except TurnRejected as exc:
        raise _rejected(exc) from exc
    except TurnVetoed as exc:
        raise ApiError(403, "vetoed", exc.reason) from exc
    except FileWriteError as exc:
        raise ApiError(413 if exc.code == "too_large" else 400, exc.code, str(exc)) from exc
    return _out(request, {"turn_id": record.turn_id, "state": record.state})


@router.get("/sessions/{session_id}/turns")
async def list_turns(session_id: str, request: Request) -> Any:
    turns = request.app.state.manager.open_turns(session_id)
    return _out(request, {"turns": [t.summary() for t in turns.list()]})


@router.get("/sessions/{session_id}/turns/{turn_id}")
async def get_turn(session_id: str, turn_id: str, request: Request) -> Any:
    record = request.app.state.manager.open_turns(session_id).get(turn_id)
    if record is None:
        raise ApiError(404, "not_found", f"unknown turn {turn_id!r}")
    return _out(request, record.model_dump(mode="json", by_alias=True))


@router.post("/sessions/{session_id}/interrupt")
async def interrupt(session_id: str, request: Request) -> Any:
    turn_id = await _live_router(request, session_id).interrupt()
    return {"interrupted": turn_id is not None, "turn_id": turn_id}


@router.post("/sessions/{session_id}/probe", status_code=202)
async def start_probe(session_id: str, request: Request, body: ProbeRequest | None = None) -> Any:
    metrics = request.app.state.metrics
    try:
        live = _live_router(request, session_id)
    except ApiError:
        if metrics is not None:
            metrics.probe_outcome("rejected")
        raise
    try:
        probe = await live.session.probe(body.text if body is not None else None)
    except TurnRejected as exc:
        if metrics is not None:
            metrics.probe_outcome("rejected")
        raise _rejected(exc) from exc
    if metrics is not None:
        metrics.probe_outcome("sent")
    return {"probe_id": probe.probe_id, "state": probe.state}


@router.get("/sessions/{session_id}/probe/{probe_id}")
async def get_probe(session_id: str, probe_id: str, request: Request) -> Any:
    probe = _live_router(request, session_id).session.get_probe(probe_id)
    if probe is None:
        raise ApiError(404, "not_found", f"unknown probe {probe_id!r}")
    return {
        "probe_id": probe.probe_id,
        "state": probe.state,
        "created_at": probe.created_at,
        "delivered_at": probe.delivered_at,
        "answered_at": probe.answered_at,
        "answer": probe.answer,
    }


def _read_lines(path: Path) -> AsyncIterator[bytes]:
    async def gen() -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        with open(path, "rb") as fh:
            while True:
                line = await loop.run_in_executor(None, fh.readline)
                if not line:
                    return
                yield line

    return gen()


@router.get("/sessions/{session_id}/transcript")
async def transcript(session_id: str, request: Request) -> StreamingResponse:
    manager = request.app.state.manager
    path = manager.transcript_path(session_id)
    if path is None or not Path(path).is_file():
        raise ApiError(404, "no_transcript", "the session has no transcript yet")
    redactor = manager.redactor

    async def body() -> AsyncIterator[bytes]:
        async for line in _read_lines(Path(path)):
            text = redactor.redact(line.decode("utf-8", errors="replace").rstrip("\n"))
            yield (text + "\n").encode("utf-8")

    return StreamingResponse(body(), media_type="application/x-ndjson")
