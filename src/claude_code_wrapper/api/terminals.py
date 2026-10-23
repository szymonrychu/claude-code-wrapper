"""Terminal routes: create, list and delete shells over HTTP and attach over a WebSocket
(part `wrapper-terminals-api`, design D2, D3).

Every check that can fail runs before the WebSocket is accepted, so a refused attach is an HTTP
denial response with the usual error envelope: the bearer (`require_auth`), the terminal id
format, the session (404 `not_found` unknown, 409 `session_not_active`) and the terminal (404
`terminal_not_found`). After accept, HTTP exception handlers no longer apply: failures are close
codes (1003 unknown control, 1009 frame over `limits.terminal_max_frame_bytes`, 1013 dropped
for falling behind, 1000 after the exit frame).

Frame protocol: binary frames are PTY bytes both ways (the scrollback snapshot first, in chunks
of at most `SEND_CHUNK_BYTES`, then live output); text frames are JSON control. Client:
`{"type":"resize","cols","rows"}` and `{"type":"ping"}`; server: `{"type":"pong"}` and
`{"type":"exit","code":N}`. A malformed or out-of-range resize is logged and ignored.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Body, Request, Response, WebSocket
from pydantic import BaseModel, ConfigDict
from starlette.websockets import WebSocketDisconnect

from claude_code_wrapper.api.errors import ApiError
from claude_code_wrapper.terminals import (
    TERMINAL_ID_RE,
    Attachment,
    InvalidSize,
    PtyClosed,
    SubscriberOverflow,
    TerminalError,
    TerminalExit,
    TerminalRegistry,
)

__all__ = ["SEND_CHUNK_BYTES", "TERMINAL_ID_RE", "TerminalError", "router"]

log = logging.getLogger(__name__)

router = APIRouter()

#: Largest binary frame the server sends (the scrollback replay is split into these).
SEND_CHUNK_BYTES = 65536
#: WebSocket close codes.
CLOSE_NORMAL = 1000
CLOSE_UNSUPPORTED = 1003
CLOSE_TOO_BIG = 1009
CLOSE_TRY_AGAIN = 1013


class CreateTerminal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: int = 24
    cols: int = 80


def _registry(app: Any) -> TerminalRegistry:
    return app.state.manager.terminals


def _api_error(exc: TerminalError) -> ApiError:
    return ApiError(exc.status, exc.code, str(exc))


def _check_terminal_id(terminal_id: str) -> None:
    """A terminal id outside the registry's format never reaches the registry or a path."""
    if not TERMINAL_ID_RE.fullmatch(terminal_id):
        raise ApiError(422, "invalid_request", "terminal_id: must match ^term_[0-9a-f]{12}$")


@router.post("/sessions/{session_id}/terminals", status_code=201)
async def create_terminal(
    session_id: str,
    request: Request,
    body: Annotated[CreateTerminal | None, Body()] = None,
) -> Any:
    size = body or CreateTerminal()
    try:
        info = await _registry(request.app).create(session_id, rows=size.rows, cols=size.cols)
    except TerminalError as exc:
        raise _api_error(exc) from exc
    return info.to_dict()


@router.get("/sessions/{session_id}/terminals")
async def list_terminals(session_id: str, request: Request) -> Any:
    request.app.state.manager.get(session_id)  # SessionNotFound (404) for an unknown session
    return {"terminals": [t.to_dict() for t in _registry(request.app).list(session_id)]}


# `:path` so a crafted id holding an encoded slash still lands here and is refused with 422.
@router.delete("/sessions/{session_id}/terminals/{terminal_id:path}", status_code=204)
async def delete_terminal(session_id: str, terminal_id: str, request: Request) -> Response:
    _check_terminal_id(terminal_id)
    request.app.state.manager.get(session_id)
    if not await _registry(request.app).close(session_id, terminal_id):
        raise ApiError(404, "terminal_not_found", f"no terminal {terminal_id} in session")
    return Response(status_code=204)


@router.websocket("/sessions/{session_id}/terminals/{terminal_id:path}/attach")
async def attach_terminal(websocket: WebSocket, session_id: str, terminal_id: str) -> None:
    _check_terminal_id(terminal_id)
    try:
        att = _registry(websocket.app).attach(session_id, terminal_id)
    except TerminalError as exc:
        raise _api_error(exc) from exc
    try:
        await websocket.accept()
        max_frame = websocket.app.state.cfg.limits.terminal_max_frame_bytes
        await _Relay(websocket, att, max_frame).run()
    finally:
        att.detach()


class _Relay:
    """One attached socket: a send loop (attachment to socket) and a receive loop (socket to
    PTY). When either ends the other is cancelled; the caller detaches."""

    def __init__(self, ws: WebSocket, att: Attachment, max_frame: int) -> None:
        self.ws = ws
        self.att = att
        self.max_frame = max_frame
        self._lock = anyio.Lock()
        self._closed = False

    async def run(self) -> None:
        """Run both loops until either ends. An anyio task group, so a cancellation from the
        caller's scope (server shutdown) stays that scope's own and no loop outlives the call."""
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._guard, self._send_loop, tg.cancel_scope)
            tg.start_soon(self._guard, self._receive_loop, tg.cancel_scope)

    async def _guard(self, loop: Callable[[], Awaitable[None]], scope: anyio.CancelScope) -> None:
        try:
            await loop()
        except (WebSocketDisconnect, OSError):
            pass  # the client went away
        except Exception:
            log.warning("terminal %s: relay failed", self.att.terminal_id, exc_info=True)
        finally:
            scope.cancel()

    async def _send(self, message: dict[str, Any]) -> None:
        async with self._lock:
            if not self._closed:
                await self.ws.send(message)

    async def _close(self, code: int) -> None:
        async with self._lock:
            if not self._closed:
                self._closed = True
                await self.ws.close(code)

    async def _control(self, frame: dict[str, Any]) -> None:
        await self._send({"type": "websocket.send", "text": json.dumps(frame)})

    async def _send_loop(self) -> None:
        snapshot = self.att.snapshot
        for start in range(0, len(snapshot), SEND_CHUNK_BYTES):
            chunk = snapshot[start : start + SEND_CHUNK_BYTES]
            await self._send({"type": "websocket.send", "bytes": chunk})
        while True:
            try:
                item = await self.att.next()
            except SubscriberOverflow:
                log.warning("terminal %s: closing a client that fell behind", self.att.terminal_id)
                await self._close(CLOSE_TRY_AGAIN)
                return
            if isinstance(item, TerminalExit):
                await self._control({"type": "exit", "code": item.code})
                await self._close(CLOSE_NORMAL)
                return
            await self._send({"type": "websocket.send", "bytes": item})

    async def _receive_loop(self) -> None:
        while True:
            message = await self.ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("bytes")
            text = message.get("text")
            size = len(data) if data is not None else len((text or "").encode())
            if size > self.max_frame:
                await self._close(CLOSE_TOO_BIG)
                return
            if data is not None:
                with contextlib.suppress(PtyClosed):  # the exit frame follows
                    await self.att.write(data)
            elif text is not None and not await self._on_control(text):
                await self._close(CLOSE_UNSUPPORTED)
                return

    async def _on_control(self, text: str) -> bool:
        """Handle one control frame; False for anything that is not a known control."""
        try:
            frame = json.loads(text)
        except ValueError:
            return False
        kind = frame.get("type") if isinstance(frame, dict) else None
        if kind == "ping":
            await self._control({"type": "pong"})
            return True
        if kind == "resize":
            self._resize(frame.get("rows"), frame.get("cols"))
            return True
        return False

    def _resize(self, rows: Any, cols: Any) -> None:
        try:
            self.att.resize(rows, cols)
        except (InvalidSize, PtyClosed) as exc:
            log.info("terminal %s: resize ignored: %s", self.att.terminal_id, exc)
