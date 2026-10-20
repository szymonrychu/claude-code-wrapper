"""Secret redaction for logs, events and API output."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

MIN_VALUE_LEN = 8
MASK = "***"

DEFAULT_PATTERNS: tuple[str, ...] = (
    r"sk-ant-[A-Za-z0-9_-]+",
    r"Bearer\s+[A-Za-z0-9._~+/=-]+",
    r"gh[pousr]_[A-Za-z0-9]{20,}",
    r"glpat-[A-Za-z0-9_-]{20,}",
)
_BEARER = re.compile(r"^Bearer\s", re.IGNORECASE)
_URL_AUTH = re.compile(r"(://[^/\s:@]+:)[^@\s/]+(@)")


class Redactor:
    """Replaces known secret values and well-known token shapes with `***`."""

    def __init__(
        self, values: Iterable[str] = (), patterns: Iterable[str] = DEFAULT_PATTERNS
    ) -> None:
        self._values: set[str] = set()
        self._patterns = [re.compile(p) for p in patterns]
        self.add(values)

    def add(self, values: Iterable[str]) -> None:
        self._values.update(v for v in values if isinstance(v, str) and len(v) >= MIN_VALUE_LEN)

    def redact(self, text: Any) -> Any:
        if not isinstance(text, str):
            return text
        for value in sorted(self._values, key=len, reverse=True):
            text = text.replace(value, MASK)
        for pattern in self._patterns:
            text = pattern.sub(self._sub, text)
        return _URL_AUTH.sub(rf"\1{MASK}\2", text)

    @staticmethod
    def _sub(match: re.Match[str]) -> str:
        return f"Bearer {MASK}" if _BEARER.match(match.group(0)) else MASK

    def redact_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {k: self.redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.redact_obj(v) for v in obj]
        if isinstance(obj, tuple):
            return tuple(self.redact_obj(v) for v in obj)
        return obj
