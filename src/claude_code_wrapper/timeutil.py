"""The one service timestamp: RFC 3339 UTC with milliseconds and a `Z` suffix."""

from __future__ import annotations

import datetime


def now_iso() -> str:
    """UTC now as RFC 3339 with milliseconds, the timestamp format of every record and event."""
    return (
        datetime.datetime.now(datetime.UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
