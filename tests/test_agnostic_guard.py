"""The wrapper stays system-agnostic: no consumer-specific vocabulary under src/."""

from __future__ import annotations

from pathlib import Path

FORBIDDEN = ("chat-send", "chat-react", "envelope", "matrix")
SRC = Path(__file__).resolve().parent.parent / "src"


def test_src_has_no_consumer_specific_words():
    hits = []
    for path in sorted(SRC.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8").lower()
        except (UnicodeDecodeError, OSError):
            continue
        hits += [f"{path.relative_to(SRC)}: {w}" for w in FORBIDDEN if w in text]
    assert not hits, hits
