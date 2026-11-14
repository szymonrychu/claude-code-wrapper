"""`GET` and `PUT /v1/managed-settings`: read and replace the overlay in flight (spec D2).

The body is never logged: it may carry settings an operator wants kept out of logs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, ValidationError

from claude_code_wrapper.api.errors import ApiError
from claude_code_wrapper.api.redaction import _out
from claude_code_wrapper.managed_settings import (
    LockedKeyError,
    ManagedSettings,
    ManagedSettingsError,
    ManagedSettingsUnavailable,
    OverlaySecretError,
)

router = APIRouter()

#: Largest PUT body accepted.
MAX_MANAGED_BODY_BYTES = 64 * 1024


class ManagedSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overlay: dict[str, Any]


def _view(ms: ManagedSettings) -> dict[str, Any]:
    return ms.status().to_dict() | {"document": ms.document}


def _unavailable() -> ApiError:
    return ApiError(
        409,
        "managed_settings_unavailable",
        "the managed settings file is not active (fallback or degraded)",
    )


@router.get("/managed-settings")
async def get_managed_settings(request: Request) -> Any:
    ms = request.app.state.manager.managed_settings
    if ms is None:
        raise _unavailable()
    ms.refresh("api")
    return _out(request, _view(ms))


@router.put("/managed-settings")
async def put_managed_settings(request: Request) -> Any:
    raw = await request.body()
    if len(raw) > MAX_MANAGED_BODY_BYTES:
        raise ApiError(413, "too_large", f"body exceeds {MAX_MANAGED_BODY_BYTES} bytes")
    try:
        body = ManagedSettingsRequest.model_validate_json(raw)
    except ValidationError as exc:
        detail = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or '<root>'}: {e['msg']}" for e in exc.errors()
        )
        raise ApiError(422, "invalid_request", detail) from None
    ms = request.app.state.manager.managed_settings
    if ms is None:
        raise _unavailable()
    try:
        ms.replace(body.overlay)
    except ManagedSettingsUnavailable:
        raise _unavailable() from None
    except LockedKeyError as exc:
        raise ApiError(422, "locked_key", str(exc)) from None
    except OverlaySecretError as exc:
        raise ApiError(422, "secret_in_overlay", str(exc)) from None
    except ManagedSettingsError as exc:
        raise ApiError(422, "invalid_overlay", str(exc)) from None
    except OSError:
        raise ApiError(
            500, "managed_settings_write_failed", "the managed settings file could not be written"
        ) from None
    return _out(request, _view(ms))
