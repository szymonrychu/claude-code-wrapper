"""Error envelope `{"error": {"code", "message"}}` and the exception handlers (D9)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from claude_code_wrapper.manager import (
    CreateFailed,
    CreateVetoed,
    InvalidRequest,
    InvalidState,
    ResumeUnavailable,
    SessionNotFound,
    TooManySessions,
)
from claude_code_wrapper.prompts import PromptError
from claude_code_wrapper.redact import Redactor


class ApiError(Exception):
    """An error the API raises itself: status, envelope code, message and optional headers."""

    def __init__(
        self, status: int, code: str, message: str, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers


def envelope(redactor: Redactor, status: int, code: str, message: str, headers=None):
    body = {"error": {"code": code, "message": redactor.redact(message)}}
    return JSONResponse(body, status_code=status, headers=headers)


def _loc(loc: tuple[Any, ...]) -> str:
    parts = [str(p) for p in loc if p not in ("body", "query", "path")]
    return ".".join(parts) or "<root>"


def install_handlers(app: FastAPI, redactor: Redactor) -> None:
    def respond(status: int, code: str, message: str, headers=None) -> JSONResponse:
        return envelope(redactor, status, code, message, headers)

    def mapped(exc_type: type[Exception], status: int, code: str | None = None) -> None:
        async def handler(_: Request, exc: Exception) -> JSONResponse:
            return respond(status, code or getattr(exc, "code", "error"), str(exc))

        app.add_exception_handler(exc_type, handler)

    mapped(SessionNotFound, 404, "not_found")
    mapped(TooManySessions, 429, "too_many_sessions")
    mapped(CreateVetoed, 403, "vetoed")  # ResumeVetoed subclasses it
    mapped(InvalidState, 409, "invalid_state")
    mapped(ResumeUnavailable, 409, "not_resumable")
    mapped(CreateFailed, 500, "create_failed")
    mapped(InvalidRequest, 422, "invalid_request")

    @app.exception_handler(PromptError)
    async def prompt_error(_: Request, exc: PromptError) -> JSONResponse:
        status = 400 if exc.code == "text_or_template" else 422
        return respond(status, exc.code, str(exc))

    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError) -> JSONResponse:
        return respond(exc.status, exc.code, exc.message, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        problems = [
            f"{_loc(tuple(e['loc']))}: {str(e['msg']).removeprefix('Value error, ')}"
            for e in exc.errors()
        ]
        return respond(422, "invalid_request", "; ".join(problems))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return respond(exc.status_code, code, str(exc.detail), dict(exc.headers or {}))
