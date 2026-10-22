"""`create_app`: the public FastAPI app (part `public-api`)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Response

from claude_code_wrapper import peer_auth
from claude_code_wrapper.api import commands, events, peers, sessions, turns
from claude_code_wrapper.api.auth import require_auth
from claude_code_wrapper.api.errors import ApiError, install_handlers
from claude_code_wrapper.config import Config
from claude_code_wrapper.metrics import CONTENT_TYPE, Metrics

log = logging.getLogger(__name__)


V1_PREFIX = "/v1"


class _HttpMetrics:
    """Pure ASGI middleware counting every response by route template, method and status."""

    def __init__(self, app: Any, metrics: Metrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = 0

        async def wrapped(message: Any) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, wrapped)
        except Exception:
            self.metrics.http_panic()
            status = status or 500
            raise
        finally:
            route = scope.get("route")
            template = getattr(route, "path", None) or "unmatched"
            # FastAPI keeps the include prefix off the route path; restore it.
            if template != "unmatched" and scope["path"].startswith(V1_PREFIX + "/"):
                template = V1_PREFIX + template
            self.metrics.http_request(template, scope["method"], status or 500)


def create_app(manager: Any, cfg: Config, metrics: Metrics | None = None) -> FastAPI:
    """Build the public API around `manager`. Routers are included on the `/v1` router."""
    cfg.server.require_auth_policy()
    app = FastAPI(title="claude-code-wrapper", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager = manager
    app.state.metrics = metrics
    app.state.cfg = cfg
    app.state.auth_token = cfg.server.auth_token
    manager.redactor.add([cfg.server.auth_token, cfg.server.callback_secret])
    install_handlers(app, manager.redactor)

    if not cfg.server.auth_token:
        log.warning(
            "server.auth_token is empty (allow_anonymous): authentication on /v1 is disabled"
        )

    v1 = APIRouter(prefix=V1_PREFIX, dependencies=[Depends(require_auth)])
    v1.include_router(sessions.router)
    v1.include_router(turns.router)
    v1.include_router(commands.router)
    v1.include_router(events.router)
    v1.include_router(peers.view_router)
    app.state.v1 = v1
    app.include_router(v1)

    if cfg.peers.enabled:
        # HMAC-signed, so deliberately outside the bearer-protected /v1 router.
        manager.redactor.add([cfg.peers.token])
        app.state.peer_nonces = peer_auth.NonceCache(cfg.peers.max_clock_skew_seconds)
        app.include_router(peers.router, prefix=V1_PREFIX)

    @app.get("/metrics")
    async def prometheus() -> Response:
        body = metrics.render() if metrics is not None else b""
        return Response(content=body, media_type=CONTENT_TYPE)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        if not manager.started:
            raise ApiError(503, "not_ready", "service is not ready")
        return {"status": "ready"}

    if metrics is not None:
        app.add_middleware(_HttpMetrics, metrics=metrics)
    return app
