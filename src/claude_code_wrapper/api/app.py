"""`create_app`: the public FastAPI app (part `public-api`)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Response

from claude_code_wrapper.api import events, sessions, turns
from claude_code_wrapper.api.auth import require_auth
from claude_code_wrapper.api.errors import ApiError, install_handlers
from claude_code_wrapper.config import Config
from claude_code_wrapper.metrics import CONTENT_TYPE, Metrics

log = logging.getLogger(__name__)


def create_app(manager: Any, cfg: Config, metrics: Metrics | None = None) -> FastAPI:
    """Build the public API around `manager`. Routers are included on the `/v1` router."""
    cfg.server.require_auth_policy()
    app = FastAPI(title="claude-code-wrapper", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager = manager
    app.state.cfg = cfg
    app.state.auth_token = cfg.server.auth_token
    manager.redactor.add([cfg.server.auth_token, cfg.server.callback_secret])
    install_handlers(app, manager.redactor)

    if not cfg.server.auth_token:
        log.warning(
            "server.auth_token is empty (allow_anonymous): authentication on /v1 is disabled"
        )

    v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_auth)])
    v1.include_router(sessions.router)
    v1.include_router(turns.router)
    v1.include_router(events.router)
    app.state.v1 = v1
    app.include_router(v1)

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

    return app
