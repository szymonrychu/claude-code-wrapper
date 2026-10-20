"""Bearer-token dependency for every /v1 route (D9)."""

from __future__ import annotations

import hmac

from fastapi import Request

from claude_code_wrapper.api.errors import ApiError


def _unauthorized() -> ApiError:
    return ApiError(
        401,
        "unauthorized",
        "missing or invalid bearer token",
        {"WWW-Authenticate": "Bearer"},
    )


async def require_auth(request: Request) -> None:
    """Reject the request unless it carries `Authorization: Bearer <server.auth_token>`.

    An empty configured token disables auth.
    """
    expected: str = request.app.state.auth_token
    if not expected:
        return
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        supplied.strip().encode(), expected.encode()
    ):
        raise _unauthorized()
