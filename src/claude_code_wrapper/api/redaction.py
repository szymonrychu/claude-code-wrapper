"""Shared response redaction for the API routers."""

from __future__ import annotations

from typing import Any

from fastapi import Request


def _out(request: Request, payload: Any) -> Any:
    """Pass a response payload through the manager's secret redactor."""
    return request.app.state.manager.redactor.redact_obj(payload)
