"""Per-session notes inbox: client-pushed notes held in memory (part `wrapper-notes-inbox`).

A keyed note replaces the previous note with the same key and is pending again; an appended
note (no key) joins a FIFO capped at `max_notes`, the oldest dropped first. Text is stored as
pushed: it is peer data, escaped only when delivery frames it. Nothing survives a restart.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .timeutil import now_iso


@dataclass
class Note:
    text: str
    key: str | None = None
    source: str | None = None
    received_at: str = ""
    delivered: bool = False


class NoteStore:
    def __init__(self, max_notes: int = 50) -> None:
        self.max_notes = max_notes
        self._keyed: dict[str, Note] = {}
        self._appended: deque[Note] = deque(maxlen=max(1, max_notes))

    def put(self, text: str, key: str | None = None, source: str | None = None) -> Note:
        note = Note(text=text, key=key, source=source, received_at=now_iso())
        if key is None:
            self._appended.append(note)
        else:
            self._keyed.pop(key, None)  # re-insert so the newest keyed note sorts last
            self._keyed[key] = note
        return note

    def notes(self) -> list[Note]:
        """Every held note, keyed first then appended, oldest first within each."""
        return [*self._keyed.values(), *self._appended]

    def pending(self) -> int:
        return sum(1 for n in self.notes() if not n.delivered)

    def mark_delivered(self, notes: list[Note]) -> None:
        for n in notes:
            n.delivered = True
