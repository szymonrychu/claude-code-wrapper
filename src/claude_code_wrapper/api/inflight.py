"""`GET /v1/sessions/{id}/inflight`: what a clear or a move of the session would lose."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from claude_code_wrapper.inflight import build_inflight

router = APIRouter()


@router.get("/sessions/{session_id}/inflight")
async def get_inflight(session_id: str, request: Request) -> Any:
    src = request.app.state.manager.inflight_source(session_id)
    return await build_inflight(src.router, src.workdir, src.since, request.app.state.cfg.pressure)
