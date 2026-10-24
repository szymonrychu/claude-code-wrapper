"""Tests for the PTY output ring (src/claude_code_wrapper/ring.py)."""

from __future__ import annotations

from claude_code_wrapper.ring import Ring


def test_ring_strips_ansi_and_whitespace():
    r = Ring()
    r.append(b"\x1b[1;31mHello\x1b[0m   \r\n\t world")
    r.append(b" \x1b]0;window title\x07after-osc ")
    r.append(b"\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\ ")
    r.append(b"\x1b=\x1b>\x1b7keys\x1b8 \x1b[?25l\x1b[2J\x1b[10;20Hend\x08\x00!")
    assert r.text() == "Hello world after-osc link keys end!"
    assert "\x1b" not in r.text()


def test_ring_text_replaces_invalid_utf8():
    r = Ring()
    r.append(b"ok \xff\xfe done")
    assert r.text() == "ok �� done"


def test_ring_capacity_drops_oldest():
    r = Ring(capacity=10)
    r.append(b"0123456789")
    r.append(b"abc")
    assert r.text() == "3456789abc"
    r.append(b"x" * 25)
    assert r.text() == "x" * 10
    assert len(Ring().text()) == 0
    big = Ring()
    big.append(b"a" * 70000 + b"tail")
    assert len(big.text()) == 65536
    assert big.text().endswith("tail")


def test_ring_reset_and_contains():
    r = Ring()
    r.append(b"\x1b[1mBypass\x1b[0m\x1b[1CPermissions\r\n   mode")
    assert r.contains("bypass permissions mode")
    assert r.contains("BYPASSPERMISSIONSMODE")
    assert r.contains("Permissions  mode")
    assert not r.contains("bypass mode")
    r.reset()
    assert r.text() == ""
    assert not r.contains("bypass")
    r.append(b"fresh")
    assert r.contains("FRESH")


def test_ring_tail():
    r = Ring()
    r.append(b"\x1b[32m" + b"a" * 1000 + b"\x1b[0m end")
    assert r.tail() == "a" * 796 + " end"
    assert len(r.tail()) == 800
    assert r.tail(3) == "end"
    assert Ring().tail() == ""


def test_ring_written_is_monotonic_across_reset():
    r = Ring(capacity=4)
    r.append(b"abcdef")
    r.append(b"gh")
    assert r.written == 8
    r.reset()
    assert r.written == 8
    r.append(b"i")
    assert r.written == 9


def test_raw_keeps_escapes_and_caps():
    r = Ring(capacity=16)
    assert r.raw() == b""
    r.append(b"\x1b[1;31mHi\x1b[0m\r\n\xff")
    assert r.raw() == b"\x1b[1;31mHi\x1b[0m\r\n\xff"
    assert isinstance(r.raw(), bytes)
    r.append(b"0123456789abcdefXYZ")
    assert r.raw() == b"3456789abcdefXYZ"[-16:]
    assert len(r.raw()) == 16
    r.append(b"\x1b[2J")
    assert r.raw().endswith(b"XYZ\x1b[2J") and len(r.raw()) == 16
    assert r.written == 16 + 19 + 4
    r.reset()
    assert r.raw() == b""
