"""Loopback-only app that receives the Claude hook relay (D7, D9).

Kept separate from the public app so a wrong bind address can never expose the hook route.
Hook tokens are compared in constant time by the manager and are never logged here.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
from collections import OrderedDict
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from claude_code_wrapper.hook_relay import (
    CLAUDE_HOOK_EVENTS,
    HEADER_DELIVERY,
    HEADER_SESSION,
    HEADER_TOKEN,
    RelayResponse,
)
from claude_code_wrapper.manager import ManagerError, SessionManager
from claude_code_wrapper.mcp_server import (
    INVALID_REQUEST,
    PARSE_ERROR,
    error_response,
    handle_message,
)

DEDUPE_LIMIT = 256
STATUSLINE_MAX_BODY = 4096
USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _optional(value: Any, kind: type | tuple[type, ...], name: str) -> Any:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, kind):
        raise ValueError(f"{name} has the wrong type")
    return value


def _finite(value: float) -> bool:
    try:
        return math.isfinite(value)
    except OverflowError:  # an int too large for a float
        return False


def _check_usage(usage: dict[str, Any] | None) -> None:
    """Token counts must be absent/null or finite non-negative numbers (never bool)."""
    for key, value in (usage or {}).items():
        if key not in USAGE_KEYS or value is None:
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not _finite(value)
            or value < 0
        ):
            raise ValueError(f"context_window.current_usage.{key} must be a finite number >= 0")


def parse_statusline(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a Claude Code statusLine payload down to the context values the router stores.

    `context_window` and its fields are null before the first assistant message and after
    /compact; null is accepted and stored as null."""
    window = _optional(payload.get("context_window"), dict, "context_window") or {}
    values = {
        "used_percentage": _optional(
            window.get("used_percentage"), (int, float), "context_window.used_percentage"
        ),
        "context_window_size": _optional(
            window.get("context_window_size"), int, "context_window.context_window_size"
        ),
        "current_usage": _optional(
            window.get("current_usage"), dict, "context_window.current_usage"
        ),
        "model": _optional(payload.get("model"), (dict, str), "model"),
    }
    pct = values["used_percentage"]
    if pct is not None and (not _finite(pct) or pct < 0):
        raise ValueError("context_window.used_percentage must be a finite number >= 0")
    size = values["context_window_size"]
    if size is not None and size < 0:
        raise ValueError("context_window.context_window_size must be >= 0")
    _check_usage(values["current_usage"])
    _check_derivable(values["current_usage"], size)
    return values


def _check_derivable(usage: dict[str, Any] | None, size: int | None) -> None:
    """The router sums the input-side counts and divides by the window; that must not overflow."""
    total = 0
    for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        total += int((usage or {}).get(key) or 0)
    try:
        float(total)
        if size:
            total / size
    except OverflowError:
        raise ValueError("context_window.current_usage token total is out of range") from None


_LOOPBACK_NAMES = frozenset({"localhost"})


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host in _LOOPBACK_NAMES:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_loopback


def _authorized_router(manager: SessionManager, request: Request) -> Any:
    """The session router for a loopback caller with a valid session id and hook token."""
    if not _is_loopback(request.client.host if request.client else None):
        raise HTTPException(403, "loopback only")
    session_id = request.headers.get(HEADER_SESSION, "")
    token = request.headers.get(HEADER_TOKEN)
    if not session_id or not manager.hook_token_matches(session_id, token):
        raise HTTPException(403, "forbidden")
    try:
        return manager.router(session_id)
    except ManagerError:
        raise HTTPException(403, "forbidden") from None


def create_internal_app(manager: SessionManager) -> FastAPI:
    app = FastAPI(title="ccw-internal", docs_url=None, redoc_url=None, openapi_url=None)
    deliveries: dict[str, OrderedDict[str, asyncio.Task[RelayResponse]]] = {}

    @app.get("/internal/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/internal/hooks/{event}")
    async def hook(event: str, request: Request) -> JSONResponse:
        if event not in CLAUDE_HOOK_EVENTS:
            raise HTTPException(404, "unknown hook event")
        router = _authorized_router(manager, request)
        session_id = request.headers[HEADER_SESSION]
        try:
            payload: Any = await request.json()
        except ValueError:
            raise HTTPException(400, "body must be a JSON object") from None
        if not isinstance(payload, dict):
            raise HTTPException(400, "body must be a JSON object")

        delivery = request.headers.get(HEADER_DELIVERY)
        if not delivery:
            return JSONResponse(await router.handle_claude_hook(event, payload))

        cache = deliveries.setdefault(session_id, OrderedDict())
        task = cache.get(delivery)
        if task is None:
            task = asyncio.ensure_future(router.handle_claude_hook(event, payload))
            cache[delivery] = task
            while len(cache) > DEDUPE_LIMIT:
                cache.popitem(last=False)
        else:
            cache.move_to_end(delivery)
        try:
            result = await asyncio.shield(task)
        except Exception:
            if cache.get(delivery) is task:
                del cache[delivery]
            raise
        return JSONResponse(result)

    @app.post("/internal/statusline")
    async def statusline(request: Request) -> Response:
        router = _authorized_router(manager, request)
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > STATUSLINE_MAX_BODY:
            raise HTTPException(413, "statusline body too large")
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > STATUSLINE_MAX_BODY:
                raise HTTPException(413, "statusline body too large")
        try:
            payload: Any = json.loads(raw)
        except ValueError:
            raise HTTPException(400, "body must be a JSON object") from None
        if not isinstance(payload, dict):
            raise HTTPException(400, "body must be a JSON object")
        try:
            values = parse_statusline(payload)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        router.on_statusline(values)
        return Response(status_code=204)

    @app.post("/internal/mcp")
    async def mcp(request: Request) -> Response:
        router = _authorized_router(manager, request)
        try:
            message: Any = await request.json()
        except ValueError:
            return JSONResponse(error_response(None, PARSE_ERROR, "invalid JSON"))
        if isinstance(message, list):
            return JSONResponse(error_response(None, INVALID_REQUEST, "batches are not supported"))
        reply = await handle_message(router, message, peers=getattr(manager, "peers", None))
        if reply is None:
            return Response(status_code=202)
        return JSONResponse(reply)

    return app
