"""Loopback-only app that receives the Claude hook relay (D7, D9).

Kept separate from the public app so a wrong bind address can never expose the hook route.
Hook tokens are compared in constant time by the manager and are never logged here.
"""

from __future__ import annotations

import asyncio
import ipaddress
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
