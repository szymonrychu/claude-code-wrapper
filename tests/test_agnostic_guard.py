"""The wrapper stays system-agnostic: no consumer-specific vocabulary under src/.

Texts, markers and endpoints a deployment needs are config keys with neutral defaults; the
deployment renders its own values (README "System agnosticism").
"""

from __future__ import annotations

from pathlib import Path

FORBIDDEN = (
    "chat",
    "chat-send",
    "chat-react",
    "envelope",
    "matrix",
    "orchestrat",
    "agents-usage",
    "agents-common",
    "tatara",
    "preset",
)
#: (path under src/, lowercase literal) -> why the literal may stay. The literal is removed
#: from that file's text before the word check, so any other hit in the file still fails.
EXEMPT = {
    ("claude_code_wrapper/pty_proc.py", "chat:clearinput"): (
        "Claude Code's own key binding name for ctrl+l, not a consumer word"
    ),
}
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
        rel = path.relative_to(SRC).as_posix()
        for (exempt_path, literal), _why in EXEMPT.items():
            if exempt_path == rel:
                text = text.replace(literal, "")
        hits += [f"{rel}: {w}" for w in FORBIDDEN if w in text]
    assert not hits, hits


def test_every_exemption_is_still_needed():
    for (rel, literal), why in EXEMPT.items():
        assert why
        assert literal in (SRC / rel).read_text(encoding="utf-8").lower(), (rel, literal)
