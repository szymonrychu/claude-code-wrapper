"""Bearer-token dependency for every /v1 route, HTTP and WebSocket alike (D9)."""

from __future__ import annotations

import hmac

from starlette.requests import HTTPConnection

from claude_code_wrapper.api.errors import ApiError


def _unauthorized() -> ApiError:
    return ApiError(
        401,
        "unauthorized",
        "missing or invalid bearer token",
        {"WWW-Authenticate": "Bearer"},
    )


async def require_auth(conn: HTTPConnection) -> None:
    """Reject the request unless it carries `Authorization: Bearer <server.auth_token>`.

    `HTTPConnection` so the one dependency guards HTTP and WebSocket routes; on a WebSocket the
    raised `ApiError` becomes a 401 denial response before accept. An empty configured token
    disables auth.
    """
    expected: str = conn.app.state.auth_token
    if not expected:
        return
    scheme, _, supplied = conn.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        supplied.strip().encode(), expected.encode()
    ):
        raise _unauthorized()
