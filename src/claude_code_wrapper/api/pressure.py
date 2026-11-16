"""`/v1/sessions/{id}/pressure`: push, read and drop a session's context pressure policy."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response

from claude_code_wrapper.api.errors import ApiError
from claude_code_wrapper.pressure import PressurePolicy, StaleVersion

router = APIRouter()


def _require_enabled(request: Request) -> None:
    """A dependency, so a disabled feature answers 409 before the body is validated (422)."""
    if not request.app.state.cfg.pressure.enabled:
        raise ApiError(
            409, "pressure_disabled", "context pressure is disabled (pressure.enabled is false)"
        )


@router.put("/sessions/{session_id}/pressure", dependencies=[Depends(_require_enabled)])
async def put_pressure(session_id: str, body: PressurePolicy, request: Request) -> Any:
    cfg = request.app.state.cfg.pressure
    if body.max_text_len() > cfg.max_text_chars:
        raise ApiError(
            422,
            "invalid_request",
            f"texts: each text must be at most {cfg.max_text_chars} characters",
        )
    manager = request.app.state.manager
    state = manager.pressure_state(session_id)
    log = manager.open_event_log(session_id)
    try:
        result = state.put(body)
    except StaleVersion as exc:
        raise ApiError(409, "stale_version", str(exc)) from exc
    if result == "set":
        log.append("pressure_policy_set", {"version": body.version, "profile": body.profile})
    level = state.level(manager.pressure_context(session_id))
    return {"version": body.version, "result": result, "level": level}


@router.get("/sessions/{session_id}/pressure")
async def get_pressure(session_id: str, request: Request) -> Any:
    manager = request.app.state.manager
    return manager.pressure_state(session_id).view(manager.pressure_context(session_id))


@router.delete("/sessions/{session_id}/pressure", status_code=204)
async def delete_pressure(session_id: str, request: Request) -> Response:
    request.app.state.manager.pressure_state(session_id).clear()
    metrics = request.app.state.metrics
    if metrics is not None:
        metrics.pressure_level_remove(session_id)
    return Response(status_code=204)
