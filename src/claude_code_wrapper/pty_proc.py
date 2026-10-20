"""Run an interactive child (claude) under a PTY, driven from the asyncio loop.

The child gets a fresh session whose controlling terminal is the pty slave, so the TUI behaves
exactly as it does for a human (the Go original uses creack/pty with Setsid and Setctty). The
wrapper never runs `claude -p`: print mode assembles a different harness.

Output is read with `loop.add_reader` on the non-blocking master, never a thread: on macOS
`close()` on a pty master blocks while another thread is parked in `read()` on it, and that
read cannot return while the child still holds the slave open. With no reader thread there is
nothing to deadlock on.

Every write goes through one `asyncio.Lock`, so a prompt (paste, pause, CR) can never be
interleaved with another writer's keystrokes.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import inspect
import logging
import os
import pty
import signal
import struct
import subprocess
import termios
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from .ring import DEFAULT_CAPACITY, Ring

log = logging.getLogger(__name__)

PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"
READ_SIZE = 65536
#: How often the exit watcher polls the child's return code.
EXIT_POLL_SECONDS = 0.05
#: How long `close()` waits for the leader to be reaped after SIGKILL.
REAP_SECONDS = 1.0

OutputCallback = Callable[[bytes], Awaitable[Any] | Any]
ExitCallback = Callable[[int], Awaitable[Any] | Any]


class PtyClosed(Exception):
    """The PTY is closed or the child has exited; nothing more can be written."""


def paste_bytes(text: str) -> bytes:
    """`text` as one bracketed paste. ESC is removed so the text cannot end the paste early."""
    return PASTE_START + text.replace("\x1b", "").encode("utf-8") + PASTE_END


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


_TIOCSCTTY = termios.TIOCSCTTY
_ioctl = fcntl.ioctl


def _acquire_ctty() -> None:
    """Runs in the forked child after setsid(): make the slave (fd 0) the controlling tty.

    Kept to one pre-bound C call; anything more is unsafe between fork and exec.
    """
    with contextlib.suppress(OSError):
        _ioctl(0, _TIOCSCTTY, 0)


class PtyProcess:
    """One child process under a PTY. `start()` once, `close()` at least once."""

    def __init__(self, ring_capacity: int = DEFAULT_CAPACITY) -> None:
        self.ring = Ring(ring_capacity)
        self.pid: int | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._fd: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()
        self._reading = False
        self._closed = False
        self._exit_fired = False
        self._close_task: asyncio.Future[None] | None = None
        self._watch_task: asyncio.Task[None] | None = None
        self._write_waiter: asyncio.Future[None] | None = None
        self._on_output: OutputCallback | None = None
        self._on_exit: ExitCallback | None = None
        self._callback_tasks: set[asyncio.Future[Any]] = set()

    @property
    def returncode(self) -> int | None:
        """The child's exit code (negative signal number when killed), None while running."""
        return self._proc.returncode if self._proc is not None else None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    async def start(
        self,
        argv: Sequence[str],
        cwd: str | os.PathLike[str],
        env: Mapping[str, str] | None,
        rows: int = 50,
        cols: int = 200,
        on_output: OutputCallback | None = None,
        on_exit: ExitCallback | None = None,
    ) -> None:
        if self._proc is not None or self._closed:
            raise RuntimeError("PtyProcess.start() may be called only once")
        loop = asyncio.get_running_loop()
        master, slave = pty.openpty()
        try:
            _set_winsize(slave, rows, cols)
            proc = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=dict(env) if env is not None else None,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                close_fds=True,
                preexec_fn=_acquire_ctty,  # one pre-bound ioctl, see _acquire_ctty
            )
        except BaseException:
            os.close(master)
            os.close(slave)
            raise
        os.close(slave)
        os.set_blocking(master, False)
        self._loop = loop
        self._proc = proc
        self.pid = proc.pid
        self._fd = master
        self._on_output = on_output
        self._on_exit = on_exit
        loop.add_reader(master, self._on_readable)
        self._reading = True
        self._watch_task = loop.create_task(self._watch_exit())

    # -- output ---------------------------------------------------------------------------

    def _on_readable(self) -> None:
        if self._fd is None:
            return
        try:
            data = os.read(self._fd, READ_SIZE)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:  # EIO: every slave fd is closed, the normal pty EOF
            data = b""
        if not data:
            self._stop_reading()
            return
        self.ring.append(data)
        self._emit(self._on_output, data)

    def _drain(self) -> None:
        """Read whatever output is still buffered, so the ring has the child's last words."""
        while self._reading and self._fd is not None:
            before = self.ring.written
            self._on_readable()
            if self.ring.written == before:
                return

    def _stop_reading(self) -> None:
        if self._reading and self._loop is not None and self._fd is not None:
            self._loop.remove_reader(self._fd)
        self._reading = False

    # -- exit -----------------------------------------------------------------------------

    async def _watch_exit(self) -> None:
        assert self._proc is not None
        while self._proc.poll() is None:
            await asyncio.sleep(EXIT_POLL_SECONDS)
        self._drain()
        self._fire_exit()

    def _fire_exit(self) -> None:
        if self._exit_fired or self._proc is None or self._proc.returncode is None:
            return
        self._exit_fired = True
        self._fail_write_waiter()
        self._emit(self._on_exit, self._proc.returncode)

    def _emit(self, callback: Callable[[Any], Any] | None, arg: Any) -> None:
        if callback is None:
            return
        try:
            result = callback(arg)
        except Exception:
            log.exception("pty callback %r failed", callback)
            return
        if inspect.isawaitable(result):
            task = asyncio.ensure_future(result)
            self._callback_tasks.add(task)
            task.add_done_callback(self._callback_done)

    def _callback_done(self, task: asyncio.Future[Any]) -> None:
        self._callback_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("pty callback failed", exc_info=task.exception())

    # -- input ----------------------------------------------------------------------------

    async def write(self, data: bytes) -> None:
        """Write all of `data`; raises `PtyClosed` after close or child exit."""
        async with self._lock:
            await self._write_locked(data)

    async def paste(self, text: str) -> None:
        """Write `text` as one bracketed paste. No CR: submitting is `send_prompt`'s job."""
        async with self._lock:
            await self._write_locked(paste_bytes(text))

    async def send_prompt(self, text: str, delay: float) -> None:
        """Paste `text`, wait `delay`, then CR as a second write, all under one lock hold.

        The CR must arrive in a later read than the paste end or the TUI takes it as a
        newline inside the paste; holding the lock keeps any other writer out of the gap.
        """
        async with self._lock:
            await self._write_locked(paste_bytes(text))
            await asyncio.sleep(delay)
            await self._write_locked(b"\r")

    def _check_writable(self) -> int:
        if self._closed or self._exit_fired or self._fd is None or self._proc is None:
            raise PtyClosed("pty is closed")
        if self._proc.poll() is not None:
            raise PtyClosed(f"child exited with {self._proc.returncode}")
        return self._fd

    async def _write_locked(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            fd = self._check_writable()
            try:
                written = os.write(fd, view)
            except (BlockingIOError, InterruptedError):
                await self._wait_writable(fd)
                continue
            except OSError as exc:
                raise PtyClosed(f"write failed: {exc}") from exc
            view = view[written:]

    async def _wait_writable(self, fd: int) -> None:
        assert self._loop is not None
        waiter: asyncio.Future[None] = self._loop.create_future()
        self._write_waiter = waiter

        def ready() -> None:
            if not waiter.done():
                waiter.set_result(None)

        self._loop.add_writer(fd, ready)
        try:
            await waiter
        finally:
            self._write_waiter = None
            if self._fd == fd:
                self._loop.remove_writer(fd)

    def _fail_write_waiter(self) -> None:
        waiter = self._write_waiter
        if waiter is not None and not waiter.done():
            waiter.set_exception(PtyClosed("pty closed while writing"))
        if waiter is not None and self._loop is not None and self._fd is not None:
            self._loop.remove_writer(self._fd)

    # -- shutdown -------------------------------------------------------------------------

    async def close(self, grace: float = 5.0) -> None:
        """Stop the child: SIGTERM its group, close the master, SIGKILL after `grace`.

        Idempotent and concurrency-safe; never raises because the child is already gone.
        """
        if self._close_task is None:
            self._close_task = asyncio.ensure_future(self._close(grace))
        await asyncio.shield(self._close_task)

    async def _close(self, grace: float) -> None:
        self._closed = True
        self._stop_reading()
        self._fail_write_waiter()
        if self._proc is None:
            return
        # Signal the group first, then close the fd, then kill: load-bearing on macOS.
        self._signal_group(signal.SIGTERM)
        self._close_fd()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace
        while self._group_alive() and loop.time() < deadline:
            await asyncio.sleep(0.02)
        if self._group_alive():
            self._signal_group(signal.SIGKILL)
        reap_deadline = loop.time() + REAP_SECONDS
        while self._proc.poll() is None and loop.time() < reap_deadline:
            await asyncio.sleep(0.01)
        self._fire_exit()
        if self._watch_task is not None and self._exit_fired:
            self._watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch_task

    def _close_fd(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)

    def _signal_group(self, sig: int) -> None:
        if self.pid is not None:
            with contextlib.suppress(OSError):
                os.killpg(self.pid, sig)

    def _group_alive(self) -> bool:
        assert self._proc is not None and self.pid is not None
        self._proc.poll()  # reap the leader: an unreaped zombie still counts as a member
        try:
            os.killpg(self.pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True
