"""Tests for the asyncio PTY child driver (src/claude_code_wrapper/pty_proc.py)."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from claude_code_wrapper import pty_proc
from claude_code_wrapper.pty_proc import PtyClosed, PtyProcess

FIXTURES = Path(__file__).parent / "fixtures" / "scenarios"

# A child that switches its terminal to raw mode, prints READY, then reports what it read.
RAW_READER = r"""
import os, sys, time, tty
tty.setraw(0)
sys.stdout.write("READY\r\n"); sys.stdout.flush()
mode = sys.argv[1]
buf = b""
if mode == "paste":
    while not buf.endswith(b"\x1b[201~"):
        buf += os.read(0, 4096)
    end = time.monotonic() + 0.4
    extra = b""
    os.set_blocking(0, False)
    while time.monotonic() < end:
        try:
            extra += os.read(0, 4096)
        except BlockingIOError:
            time.sleep(0.02)
    sys.stdout.write("GOT=" + buf.hex() + " EXTRA=" + extra.hex() + " DONE\r\n")
else:
    want = int(mode)
    while len(buf) < want:
        buf += os.read(0, 65536)
    ok = buf == bytes(i % 251 for i in range(want))
    sys.stdout.write("COUNT=%d OK=%s DONE\r\n" % (len(buf), ok))
sys.stdout.flush()
"""


async def wait_until(pred: Callable[[], bool], timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def py(code: str, *args: str) -> list[str]:
    return [sys.executable, "-I", "-c", code, *args]


@pytest.fixture
async def spawn(tmp_path):
    made: list[PtyProcess] = []

    async def _spawn(argv, env=None, **kw) -> PtyProcess:
        p = PtyProcess()
        made.append(p)
        await p.start(argv, str(tmp_path), env if env is not None else dict(os.environ), **kw)
        return p

    yield _spawn
    for p in made:
        await p.close(grace=1.0)


def gone(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


async def test_output_callback_receives_bytes(spawn):
    chunks: list[bytes] = []
    p = await spawn(
        ["/bin/sh", "-c", "printf 'hello \\033[31mred\\033[0m world'"], on_output=chunks.append
    )
    await wait_until(lambda: b"world" in b"".join(chunks))
    assert b"\x1b[31mred" in b"".join(chunks)
    assert p.ring.text().endswith("hello red world")
    assert isinstance(p.pid, int) and p.pid > 0


async def test_window_size_and_controlling_tty(spawn):
    code = (
        "import os; s = os.get_terminal_size(0); fd = os.open('/dev/tty', os.O_RDWR);"
        "print('SIZE=%dx%d' % (s.lines, s.columns), 'FG=%s' % (os.tcgetpgrp(fd) == os.getpgrp()),"
        "'LEADER=%s' % (os.getsid(0) == os.getpid()))"
    )
    p = await spawn(py(code), rows=33, cols=120)
    await wait_until(lambda: p.ring.contains("LEADER="))
    assert p.ring.contains("SIZE=33x120 FG=True LEADER=True"), p.ring.text()


# Prints the size on every SIGWINCH, so a resize is visible from outside.
WINCH_REPORTER = r"""
import os, signal, sys, time
def report(*_):
    s = os.get_terminal_size(0)
    sys.stdout.write("WINSZ=%dx%d\n" % (s.lines, s.columns)); sys.stdout.flush()
signal.signal(signal.SIGWINCH, report)
sys.stdout.write("READY\n"); sys.stdout.flush()
time.sleep(30)
"""


async def test_resize_reaches_child(spawn):
    p = await spawn(py(WINCH_REPORTER), rows=24, cols=80)
    await wait_until(lambda: p.ring.contains("READY"))
    p.resize(40, 132)
    await wait_until(lambda: p.ring.contains("WINSZ=40x132"))
    p.resize(10, 20)
    await wait_until(lambda: p.ring.contains("WINSZ=10x20"))


async def test_resize_rejects_bad_sizes_and_states(spawn):
    fresh = PtyProcess()
    with pytest.raises(RuntimeError):
        fresh.resize(24, 80)
    p = await spawn(["/bin/cat"])
    for rows, cols in [(0, 80), (24, 0), (-1, 80), (24, -5), (1001, 80), (24, 1001)]:
        with pytest.raises(ValueError):
            p.resize(rows, cols)
    p.resize(1, 1)
    p.resize(1000, 1000)
    await p.close(grace=1.0)
    with pytest.raises(RuntimeError):
        p.resize(24, 80)


async def test_write_round_trip_cat(spawn):
    p = await spawn(["/bin/cat"])
    await p.write(b"ping-pong\n")
    # The tty echoes the input and cat prints the line back: two copies.
    await wait_until(lambda: p.ring.text().count("ping-pong") == 2)
    assert p.alive


async def test_write_handles_partial_writes(spawn):
    size = 200_000
    p = await spawn(py(RAW_READER, str(size)))
    await wait_until(lambda: p.ring.contains("READY"))
    await p.write(bytes(i % 251 for i in range(size)))
    await wait_until(lambda: p.ring.contains("DONE"), timeout=20)
    assert p.ring.contains(f"COUNT={size} OK=True"), p.ring.tail()


async def test_paste_wraps_bracketed(spawn):
    p = await spawn(py(RAW_READER, "paste"))
    await wait_until(lambda: p.ring.contains("READY"))
    await p.paste("hel\x1blo\nwörld")
    await wait_until(lambda: p.ring.contains("DONE"))
    expected = b"\x1b[200~hello\nw\xc3\xb6rld\x1b[201~"
    assert p.ring.contains(f"GOT={expected.hex()} EXTRA= DONE"), p.ring.tail()


async def test_exit_callback_returncode(spawn):
    exits: list[int] = []
    p = await spawn(["/bin/sh", "-c", "exit 3"], on_exit=exits.append)
    await wait_until(lambda: exits)
    assert exits == [3]
    assert p.returncode == 3
    assert not p.alive
    await p.close(grace=0.5)
    await asyncio.sleep(0.2)
    assert exits == [3]


async def test_async_exit_callback_is_awaited(spawn):
    seen: list[int] = []

    async def on_exit(code: int) -> None:
        await asyncio.sleep(0)
        seen.append(code)

    await spawn(["/bin/sh", "-c", "exit 9"], on_exit=on_exit)
    await wait_until(lambda: seen)
    assert seen == [9]


async def test_eof_before_exit_waits_for_returncode(spawn):
    exits: list[int] = []
    script = "exec 0<&- 1>&- 2>&-; sleep 0.5; exit 4"
    p = await spawn(["/bin/sh", "-c", script], on_exit=exits.append)
    await asyncio.sleep(0.25)
    assert exits == []
    await wait_until(lambda: exits)
    assert exits == [4]
    assert p.returncode == 4


async def test_exit_without_eof_still_fires(spawn):
    # The backgrounded sleep keeps the slave open, so there is no EOF; the exit must still fire.
    exits: list[int] = []
    p = await spawn(["/bin/sh", "-c", "sleep 30 & exit 5"], on_exit=exits.append)
    await wait_until(lambda: exits)
    assert exits == [5]
    with pytest.raises(PtyClosed):
        await p.write(b"x")
    await p.close(grace=1.0)
    await wait_until(lambda: gone(p.pid), timeout=3)


async def test_close_terminates_group_then_kill(spawn, monkeypatch):
    calls: list[tuple] = []
    real_killpg, real_close = os.killpg, os.close
    # HUP is ignored too: closing the master hangs up the child's controlling tty.
    p = await spawn(["/bin/sh", "-c", 'trap "" TERM HUP; sleep 60 & echo CHILD=$!; wait'])
    await wait_until(lambda: p.ring.contains("CHILD="))
    child_pid = int(p.ring.text().split("CHILD=")[1].split()[0])
    master = p._fd

    def killpg(pid, sig):
        if pid == p.pid and sig != 0:
            calls.append(("killpg", sig))
        return real_killpg(pid, sig)

    def close(fd):
        if fd == master:
            calls.append(("close", fd))
        return real_close(fd)

    monkeypatch.setattr(pty_proc.os, "killpg", killpg)
    monkeypatch.setattr(pty_proc.os, "close", close)
    started = time.monotonic()
    await p.close(grace=0.5)
    elapsed = time.monotonic() - started
    monkeypatch.undo()

    assert calls == [("killpg", signal.SIGTERM), ("close", master), ("killpg", signal.SIGKILL)]
    assert 0.5 <= elapsed < 1.5
    assert not p.alive
    assert p.returncode == -signal.SIGKILL
    await wait_until(lambda: gone(p.pid), timeout=2)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


async def test_close_stops_term_respecting_child_without_kill(spawn, monkeypatch):
    sigs: list[int] = []
    real_killpg = os.killpg
    p = await spawn(["/bin/sleep", "60"])

    def killpg(pid, sig):
        if sig != 0:
            sigs.append(sig)
        return real_killpg(pid, sig)

    monkeypatch.setattr(pty_proc.os, "killpg", killpg)
    started = time.monotonic()
    await p.close(grace=5.0)
    assert time.monotonic() - started < 2.0
    assert sigs == [signal.SIGTERM]
    assert p.returncode == -signal.SIGTERM


async def test_close_shorter_grace_escalates(spawn):
    # A close already waiting out a long grace is hurried by a later close with a short one.
    p = await spawn(["/bin/sh", "-c", 'trap "" TERM HUP; sleep 60 & echo CHILD=$!; wait'])
    await wait_until(lambda: p.ring.contains("CHILD="))
    slow = asyncio.ensure_future(p.close(grace=30.0))
    await asyncio.sleep(0.2)
    assert not slow.done()
    started = time.monotonic()
    await p.close(grace=0)
    assert time.monotonic() - started < 1.5
    await asyncio.wait_for(slow, 1.0)
    assert not p.alive
    assert p.returncode == -signal.SIGKILL
    # A longer grace never postpones a kill already due.
    q = await spawn(["/bin/sh", "-c", 'trap "" TERM HUP; sleep 60 & echo CHILD=$!; wait'])
    await wait_until(lambda: q.ring.contains("CHILD="))
    started = time.monotonic()
    await asyncio.gather(q.close(grace=0.3), q.close(grace=30.0))
    assert time.monotonic() - started < 1.5


async def test_close_idempotent(spawn):
    exits: list[int] = []
    p = await spawn(["/bin/sh", "-c", "exit 0"], on_exit=exits.append)
    await wait_until(lambda: exits)
    await p.close(grace=0.5)
    await p.close(grace=0.5)
    q = await spawn(["/bin/sleep", "60"], on_exit=exits.append)
    await asyncio.gather(q.close(grace=0.5), q.close(grace=0.5))
    await q.close()
    assert exits == [0, -signal.SIGTERM]
    assert not q.alive
    await PtyProcess().close()


async def test_write_after_close_raises(spawn):
    p = await spawn(["/bin/cat"])
    await p.close(grace=0.5)
    with pytest.raises(PtyClosed):
        await p.write(b"x")
    with pytest.raises(PtyClosed):
        await p.paste("x")
    with pytest.raises(PtyClosed):
        await p.send_prompt("x", 0)
    with pytest.raises(PtyClosed):
        await PtyProcess().write(b"x")


async def test_write_after_exit_raises(spawn):
    exits: list[int] = []
    p = await spawn(["/bin/sh", "-c", "exit 0"], on_exit=exits.append)
    await wait_until(lambda: exits)
    with pytest.raises(PtyClosed):
        await p.write(b"x")


async def test_close_unblocks_pending_write(spawn):
    # The raw child never reads, so a big write blocks on a full buffer until close fails it.
    p = await spawn(
        py("import tty, time; tty.setraw(0); print('READY', flush=True); time.sleep(60)")
    )
    await wait_until(lambda: p.ring.contains("READY"))
    pending = asyncio.ensure_future(p.write(b"z" * 1_000_000))
    await asyncio.sleep(0.2)
    assert not pending.done()
    await p.close(grace=0.5)
    with pytest.raises(PtyClosed):
        await asyncio.wait_for(pending, 2)


async def test_send_prompt_two_writes_under_one_lock(spawn, monkeypatch):
    p = await spawn(["/bin/cat"])
    master = p._fd
    writes: list[tuple[float, bytes]] = []
    real_write = os.write

    def write(fd, data):
        if fd == master:
            writes.append((time.monotonic(), bytes(data)))
        return real_write(fd, data)

    monkeypatch.setattr(pty_proc.os, "write", write)

    async def intruder():
        await asyncio.sleep(0.05)
        await p.write(b"X")

    await asyncio.gather(p.send_prompt("do \x1bit", 0.3), intruder())
    assert [w for _, w in writes] == [b"\x1b[200~do it\x1b[201~", b"\r", b"X"]
    assert writes[1][0] - writes[0][0] >= 0.29


async def test_fake_claude_boots_under_pty(spawn, fake_claude_bin, claude_config_dir, tmp_path):
    env = {
        "PATH": os.environ["PATH"],
        "CLAUDE_CONFIG_DIR": str(claude_config_dir),
        "FAKE_CLAUDE_SCENARIO": str(FIXTURES / "basic.json"),
    }
    exits: list[int] = []
    p = await spawn([str(fake_claude_bin)], env=env, on_exit=exits.append)
    await wait_until(lambda: p.ring.contains("Bypass Permissions mode"))
    assert p.ring.contains("1. No, exit")
    assert "\x1b" not in p.ring.text()
    await asyncio.sleep(0.4)
    await p.write(b"\x1b[B")
    await asyncio.sleep(0.1)
    await p.write(b"\r")
    await wait_until(lambda: p.ring.contains("? for shortcuts"))
    await asyncio.sleep(0.2)
    await p.send_prompt("hi there", 0.2)
    await wait_until(lambda: p.ring.contains("All done."))
    await p.write(b"\x03")
    await wait_until(lambda: exits)
    assert exits == [0]
