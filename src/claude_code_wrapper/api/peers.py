"""Receiving peer routes (part `peer-messages-endpoint`, design D5/D6/D13).

Authenticated by HMAC request signatures, never by a bearer token. Mounted only when
`peers.enabled`, without the `/v1` bearer dependency.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

from claude_code_wrapper import peer_auth
from claude_code_wrapper.api.errors import ApiError
from claude_code_wrapper.peer_client import (
    RECEIVER_STATUS,
    MemberView,
    MessageBody,
    NoticeBody,
    PeerError,
)

log = logging.getLogger(__name__)

BODY_SLACK = 4096
router = APIRouter(prefix="/peers")


class PeerReceiver(Protocol):
    """What the routes call: `request.app.state.manager.peers`."""

    async def accept_message(self, body: MessageBody) -> dict[str, Any]: ...

    async def accept_notice(self, body: NoticeBody) -> dict[str, Any]: ...

    async def accept_heartbeat(self, view: MemberView) -> MemberView: ...


def _too_large() -> ApiError:
    return ApiError(413, "message_too_large", "peer request body is too large")


def _count(request: Request, kind: str, result: str) -> None:
    metrics = request.app.state.metrics
    if metrics is not None:
        metrics.peer_received(kind, result)


def _membership(request: Request, result: str) -> None:
    metrics = request.app.state.metrics
    if metrics is not None:
        metrics.peer_membership("inbound_heartbeat", result)


async def _read_capped(request: Request, cap: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > cap:
        raise _too_large()
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > cap:
            raise _too_large()
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_verified(request: Request, kind: str | None) -> bytes:
    """Read the capped raw body, then authenticate it. `kind` is the metrics kind, if any."""
    cfg = request.app.state.cfg.peers
    try:
        raw = await _read_capped(request, cfg.max_message_bytes + BODY_SLACK)
    except ApiError:
        if kind:
            _count(request, kind, "too_large")
        raise
    try:
        peer_auth.verify_request(
            cfg.token,
            "POST",
            request.url.path,
            request.headers,
            raw,
            request.app.state.peer_nonces,
            skew_seconds=cfg.max_clock_skew_seconds,
        )
    except peer_auth.PeerAuthError as exc:
        log.debug("peer request rejected: %s", exc.reason)
        if kind:
            _count(request, kind, "unauthorized")
        else:
            _membership(request, "failed")
        raise ApiError(401, "unauthorized", "peer authentication failed") from None
    except peer_auth.NonceCacheFull:
        raise ApiError(429, "rate_limited", "too many peer requests") from None
    return raw


def _parse[M: BaseModel](raw: bytes, model: type[M]) -> M:
    try:
        return model.model_validate(json.loads(raw))
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from None
    except ValueError:
        raise RequestValidationError(
            [{"loc": ("body",), "msg": "invalid JSON", "type": "json_invalid"}]
        ) from None


def _api_error(exc: PeerError) -> ApiError:
    return ApiError(RECEIVER_STATUS.get(exc.code, 500), exc.code, exc.message)


def _receiver(request: Request) -> PeerReceiver:
    return request.app.state.manager.peers


async def _push(
    request: Request, kind: str, model: type[MessageBody | NoticeBody], call: str
) -> dict[str, Any]:
    raw = await _read_verified(request, kind)
    body = _parse(raw, model)
    if isinstance(body, MessageBody):
        limit = request.app.state.cfg.peers.max_message_bytes
        if len(body.message.encode("utf-8")) > limit:
            _count(request, kind, "too_large")
            raise _too_large()
    try:
        return await getattr(_receiver(request), call)(body)
    except PeerError as exc:
        if exc.code == "not_found":
            _count(request, kind, "not_found")
        raise _api_error(exc) from None


@router.post("/messages", status_code=202)
async def post_message(request: Request) -> dict[str, Any]:
    return await _push(request, "message", MessageBody, "accept_message")


@router.post("/notices", status_code=202)
async def post_notice(request: Request) -> dict[str, Any]:
    return await _push(request, "notice", NoticeBody, "accept_notice")


@router.post("/heartbeat")
async def post_heartbeat(request: Request) -> Any:
    raw = await _read_verified(request, None)
    view = _parse(raw, MemberView)
    try:
        answer = await _receiver(request).accept_heartbeat(view)
    except PeerError as exc:
        _membership(request, "failed")
        raise _api_error(exc) from None
    _membership(request, "ok")
    return answer.model_dump(by_alias=True)
