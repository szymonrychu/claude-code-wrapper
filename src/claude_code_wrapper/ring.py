"""The last N bytes of PTY output, readable as clean screen text.

This is the only window into the interactive TUI and it is deliberately a narrow one: boot
dialog detection, fatal-pattern checks and a tail for debug logging. Turn results never come
from here - they come from the Stop hook and the transcript.

The TUI lays words out with cursor-movement escapes rather than literal spaces, so matching
(`contains`) ignores whitespace entirely and case as well.
"""

from __future__ import annotations

import re
import threading

DEFAULT_CAPACITY = 64 * 1024

# CSI: ESC [ params intermediates final.
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# OSC: ESC ] ... terminated by BEL or ST (ESC \); an unterminated one runs to the end.
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
# Any other escape: ESC, optional intermediates, one final byte (ESC 7, ESC =, ESC ( B ...).
_OTHER = re.compile(r"\x1b[ -/]*[0-~]")
# Remaining control characters other than whitespace, plus any stray ESC.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x1b]")
_WS = re.compile(r"\s+")


def strip_ansi(text: str) -> str:
    """Remove CSI, OSC and other escape sequences and non-whitespace control characters."""
    text = _CSI.sub("", text)
    text = _OSC.sub("", text)
    text = _OTHER.sub("", text)
    return _CONTROL.sub("", text)


def _squash(text: str) -> str:
    return _WS.sub("", text).casefold()


class Ring:
    """Keeps the newest `capacity` bytes of output. Safe to share across threads."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._buf = bytearray()
        self._written = 0
        self._lock = threading.Lock()

    def append(self, data: bytes) -> None:
        with self._lock:
            self._buf.extend(data)
            overflow = len(self._buf) - self.capacity
            if overflow > 0:
                del self._buf[:overflow]
            self._written += len(data)

    def reset(self) -> None:
        """Empty the buffer (before a relaunch, so stale dialog text cannot match).

        `written` stays monotonic so output-quiescence checks keep a valid baseline.
        """
        with self._lock:
            self._buf.clear()

    @property
    def written(self) -> int:
        """Total bytes ever appended, unaffected by capacity or `reset()`."""
        with self._lock:
            return self._written

    def text(self) -> str:
        """The buffer decoded (errors replaced), escapes removed, whitespace runs collapsed."""
        with self._lock:
            raw = bytes(self._buf)
        return _WS.sub(" ", strip_ansi(raw.decode("utf-8", "replace")))

    def contains(self, needle: str) -> bool:
        """Case- and whitespace-insensitive substring match against `text()`."""
        return _squash(needle) in _squash(self.text())

    def tail(self, n: int = 800) -> str:
        """The last `n` characters of `text()`."""
        if n <= 0:
            return ""
        return self.text()[-n:]
