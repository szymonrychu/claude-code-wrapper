"""Interactive login shells in a session workdir (part `terminal-registry`, design D2, D3, D11).

The `TerminalRegistry` (held by the `SessionManager` as `manager.terminals`) is the in-process
owner of every shell: it starts them under a `PtyProcess`, fans each shell's output out to any
number of subscribers, reaps the idle ones and closes them with their session.

Concurrency model: everything runs on the event loop and every state change below is one
synchronous step (no await inside), which is what makes the guarantees hold:

- `create` checks the cap and registers a pending slot in one step before awaiting the start,
  and releases the slot when the start fails, so concurrent creates never exceed the cap.
- `attach` takes the scrollback snapshot (`Ring.raw()`) and subscribes in one step; the PTY
  appends to the ring and fans out in one step too, so replay and live output join exactly.
- The output callback never awaits: each subscriber has a byte-bounded queue fed with
  `put_nowait`; a subscriber that falls behind is marked overflowed and dropped, never the PTY.
- `close`, the reaper, session close and shutdown unregister synchronously before awaiting the
  kill, so an attach racing them gets `TerminalNotFound` or (attached first) the exit marker.

A close kills every process of the shell's session (its session id is the shell pid, found
through `/proc/<pid>/stat`), not only its process group, so a `nohup`'d job does not survive.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import secrets
import signal
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from .claude_env import ClaudeEnv
from .config import LimitsConfig
from .files import resolve_in_workdir
from .pty_proc import MAX_DIMENSION, PtyClosed, PtyProcess
from .ring import Ring
from .timeutil import now_iso

__all__ = [
    "SCRUBBED_ENV",
    "TERMINAL_ID_RE",
    "Attachment",
    "InvalidSize",
    "PtyClosed",
    "SessionNotActive",
    "ShellTarget",
    "SubscriberOverflow",
    "TerminalError",
    "TerminalExit",
    "TerminalInfo",
    "TerminalLimit",
    "TerminalNotFound",
    "TerminalRegistry",
    "login_shell_argv",
    "shell_env",
]

log = logging.getLogger(__name__)

#: The terminal id format (`term_` and 12 lowercase hex digits).
TERMINAL_ID_RE = re.compile(r"^term_[0-9a-f]{12}$")
#: Never in a shell env: every secret-kind Claude variable and the service's own tokens.
SCRUBBED_ENV = frozenset(
    {s.env for s in ClaudeEnv.catalogue() if s.secret}
    | {"CCW_AUTH_TOKEN", "CCW_PEER_TOKEN", "CCW_HOOK_TOKEN"}
)
SHELL_TERM = "xterm-256color"
#: Most bytes waiting in one subscriber's queue before it is dropped as overflowed.
DEFAULT_QUEUE_BYTES = 1024 * 1024
#: Longest pause between reaper ticks.
REAP_INTERVAL_SECONDS = 30.0
#: SIGTERM-to-SIGKILL grace of a shell close.
CLOSE_GRACE_SECONDS = 1.0
#: How long a close keeps SIGKILLing what is left of the shell's session.
SESSION_KILL_SECONDS = 1.0


class TerminalError(Exception):
    """A terminal failure; `code` and `status` are what the API answers."""

    code = "terminal_error"
    status = 500


class TerminalLimit(TerminalError):
    code = "terminal_limit"
    status = 409

    def __init__(self, limit: int) -> None:
        super().__init__(f"at most {limit} terminals per service process")
        self.limit = limit


class TerminalNotFound(TerminalError):
    code = "terminal_not_found"
    status = 404

    def __init__(self, session_id: str, terminal_id: str) -> None:
        super().__init__(f"no terminal {terminal_id} in session {session_id}")
        self.session_id = session_id
        self.terminal_id = terminal_id


class SessionNotActive(TerminalError):
    """The session is known but not running, so it has no shells."""

    code = "session_not_active"
    status = 409

    def __init__(self, session_id: str, state: str | None = None) -> None:
        detail = f" (state {state})" if state else ""
        super().__init__(f"session {session_id} is not active{detail}")
        self.session_id = session_id
        self.state = state


class InvalidSize(TerminalError, ValueError):
    code = "invalid_size"
    status = 422

    def __init__(self, rows: object, cols: object) -> None:
        super().__init__(f"rows and cols must be 1..{MAX_DIMENSION}, got {rows}x{cols}")


class SubscriberOverflow(TerminalError):
    """The subscriber fell behind by more than its queue holds and was dropped."""

    code = "subscriber_overflow"
    status = 1013  # the WebSocket close code the API uses


@dataclass(frozen=True)
class TerminalExit:
    """The exit marker: the last item a subscriber receives. `code` is None when unknown."""

    code: int | None


@dataclass(frozen=True)
class ShellTarget:
    """Where and with what a session's shell runs (built by the SessionManager)."""

    workdir: str
    env: Mapping[str, str]


@dataclass(frozen=True)
class TerminalInfo:
    terminal_id: str
    session_id: str
    created_at: str
    alive: bool
    exit_code: int | None
    attached: int
    rows: int
    cols: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def login_shell_argv() -> tuple[str, ...]:
    """`/bin/bash -l`, or `/bin/sh -l` where bash is absent (a login shell reads profile.d)."""
    return ("/bin/bash", "-l") if os.path.exists("/bin/bash") else ("/bin/sh", "-l")


def shell_env(claude_env: Mapping[str, str]) -> dict[str, str]:
    """The Claude process env minus every scrubbed name, with `TERM` for xterm.js."""
    env = {k: v for k, v in claude_env.items() if k not in SCRUBBED_ENV}
    env["TERM"] = SHELL_TERM
    return env


def _check_size(rows: int, cols: int) -> None:
    ok = all(
        isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= MAX_DIMENSION
        for v in (rows, cols)
    )
    if not ok:
        raise InvalidSize(rows, cols)


def _session_members(sid: int) -> list[int]:
    """Pids of the live (non-zombie) processes whose session id is `sid`; [] without /proc."""
    members: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return members
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="ascii", errors="replace") as fh:
                stat = fh.read()
        except OSError:
            continue
        fields = stat.rpartition(")")[2].split()
        if len(fields) > 3 and fields[3] == str(sid) and fields[0] != "Z":
            members.append(int(entry))
    return members


def _signal_all(pids: Sequence[int], sig: int) -> None:
    for pid in pids:
        if pid != os.getpid():
            with contextlib.suppress(OSError):
                os.kill(pid, sig)


class _Terminal:
    def __init__(
        self, terminal_id: str, session_id: str, rows: int, cols: int, scrollback: int, now: float
    ) -> None:
        self.terminal_id = terminal_id
        self.session_id = session_id
        self.rows = rows
        self.cols = cols
        self.created_at = now_iso()
        self.pty = PtyProcess(ring_capacity=scrollback)
        self.subscribers: set[Attachment] = set()
        self.started = False
        self.exited = False
        self.exit_code: int | None = None
        self.idle_since = now

    def info(self) -> TerminalInfo:
        return TerminalInfo(
            terminal_id=self.terminal_id,
            session_id=self.session_id,
            created_at=self.created_at,
            alive=not self.exited and self.pty.alive,
            exit_code=self.exit_code,
            attached=len(self.subscribers),
            rows=self.rows,
            cols=self.cols,
        )


@dataclass(eq=False)
class Attachment:
    """One subscriber of a terminal: `snapshot` first, then `next()` until the exit marker.

    `next()` returns live output bytes or the `TerminalExit` marker, and raises
    `SubscriberOverflow` once the subscriber was dropped for falling behind.
    """

    _registry: TerminalRegistry
    _term: _Terminal
    snapshot: bytes
    _limit: int
    overflowed: bool = False
    _queue: asyncio.Queue[bytes | TerminalExit | None] = field(default_factory=asyncio.Queue)
    _pending: int = 0
    _ended: bool = False

    @property
    def terminal_id(self) -> str:
        return self._term.terminal_id

    async def next(self) -> bytes | TerminalExit:
        item = await self._queue.get()
        if item is None:
            raise SubscriberOverflow(f"subscriber of {self.terminal_id} fell behind")
        if isinstance(item, bytes):
            self._pending -= len(item)
        return item

    async def write(self, data: bytes) -> None:
        """Input for the shell; `PtyClosed` once it exited or was closed."""
        await self._term.pty.write(data)

    def resize(self, rows: int, cols: int) -> None:
        """`InvalidSize` outside 1..1000; `PtyClosed` once the shell is closed."""
        _check_size(rows, cols)
        try:
            self._term.pty.resize(rows, cols)
        except RuntimeError as exc:
            raise PtyClosed(str(exc)) from exc
        self._term.rows, self._term.cols = rows, cols

    def detach(self) -> None:
        """Stop receiving; idempotent. The shell keeps running."""
        self._registry._unsubscribe(self._term, self)

    # -- fed by the registry, always synchronously on the loop ---------------------------

    def _feed(self, data: bytes) -> bool:
        """Queue `data`; False (and marked overflowed) when it does not fit. A caught-up
        subscriber (nothing pending) always takes the chunk, however large."""
        if self._ended:
            return True
        if self._pending and self._pending + len(data) > self._limit:
            self._overflow()
            return False
        self._pending += len(data)
        self._queue.put_nowait(data)
        return True

    def _overflow(self) -> None:
        self.overflowed = True
        self._ended = True
        while not self._queue.empty():
            self._queue.get_nowait()
        self._pending = 0
        self._queue.put_nowait(None)

    def _finish(self, code: int | None) -> None:
        if not self._ended:
            self._ended = True
            self._queue.put_nowait(TerminalExit(code))


class TerminalRegistry:
    """Every shell of this service process, keyed by terminal id.

    `resolve(session_id)` returns the `ShellTarget` of an active session and raises the
    manager's `SessionNotFound` for an unknown session or `SessionNotActive` for a known one
    that is not running. `clock` is the monotonic clock of the idle reaper.
    """

    def __init__(
        self,
        limits: LimitsConfig,
        resolve: Callable[[str], ShellTarget],
        *,
        argv: Sequence[str] | None = None,
        reap_interval: float | None = None,
        queue_bytes: int = DEFAULT_QUEUE_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits
        self._resolve = resolve
        self.shell_argv: tuple[str, ...] = tuple(argv) if argv else login_shell_argv()
        idle = limits.terminal_idle_reap_seconds
        self._reap_interval = (
            reap_interval if reap_interval is not None else min(REAP_INTERVAL_SECONDS, idle / 2)
        )
        self._queue_bytes = queue_bytes
        self._clock = clock
        self._terminals: dict[str, _Terminal] = {}
        self._reaper: asyncio.Task[None] | None = None
        self._closed = False

    # ------------------------------------------------------------------ lookup

    def count(self) -> int:
        """Registered terminals, alive or exited, including creates still starting."""
        return len(self._terminals)

    @property
    def reaper_running(self) -> bool:
        return self._reaper is not None and not self._reaper.done()

    def list(self, session_id: str) -> list[TerminalInfo]:
        return [
            t.info() for t in self._terminals.values() if t.started and t.session_id == session_id
        ]

    def get(self, session_id: str, terminal_id: str) -> TerminalInfo:
        return self._find(session_id, terminal_id).info()

    def pid(self, session_id: str, terminal_id: str) -> int | None:
        """The shell's pid (also its process group and session id)."""
        return self._find(session_id, terminal_id).pty.pid

    def ring(self, session_id: str, terminal_id: str) -> Ring:
        """The shell's scrollback ring."""
        return self._find(session_id, terminal_id).pty.ring

    def _find(self, session_id: str, terminal_id: str) -> _Terminal:
        term = self._terminals.get(terminal_id)
        if term is None or not term.started or term.session_id != session_id:
            raise TerminalNotFound(session_id, terminal_id)
        return term

    # ------------------------------------------------------------------ create

    async def create(self, session_id: str, rows: int = 24, cols: int = 80) -> TerminalInfo:
        """Start a login shell in the session workdir.

        `InvalidSize` for rows or cols outside 1..1000, `TerminalLimit` at the cap, the
        resolver's `SessionNotFound` or `SessionNotActive` for a session without shells.
        """
        _check_size(rows, cols)
        if self._closed:
            raise SessionNotActive(session_id, "service shutting down")
        target = self._resolve(session_id)
        cwd = resolve_in_workdir(target.workdir, "")
        env = shell_env(target.env)
        # Reserve the slot: cap check and registration in one synchronous step.
        if len(self._terminals) >= self.limits.terminals_max:
            raise TerminalLimit(self.limits.terminals_max)
        terminal_id = self._new_id()
        term = _Terminal(
            terminal_id,
            session_id,
            rows,
            cols,
            self.limits.terminal_scrollback_bytes,
            self._clock(),
        )
        self._terminals[terminal_id] = term
        try:
            await term.pty.start(
                self.shell_argv,
                cwd,
                env,
                rows=rows,
                cols=cols,
                on_output=lambda data: self._on_output(term, data),
                on_exit=lambda code: self._on_exit(term, code),
            )
        except BaseException:
            if self._terminals.get(terminal_id) is term:
                del self._terminals[terminal_id]
            raise
        if self._terminals.get(terminal_id) is not term:  # closed while starting
            await self._terminate(term, 0)
            raise SessionNotActive(session_id, "closed while the shell started")
        term.started = True
        self._ensure_reaper()
        log.info("session %s: terminal %s started (pid %s)", session_id, terminal_id, term.pty.pid)
        return term.info()

    def _new_id(self) -> str:
        while True:
            terminal_id = f"term_{secrets.token_hex(6)}"
            if terminal_id not in self._terminals:
                return terminal_id

    # ------------------------------------------------------------------ attach

    def attach(self, session_id: str, terminal_id: str) -> Attachment:
        """Subscribe to a terminal; its scrollback snapshot and the subscription are one step.

        An exited terminal replays its scrollback, then the exit marker.
        """
        if self._closed:
            raise SessionNotActive(session_id, "service shutting down")
        self._resolve(session_id)
        term = self._find(session_id, terminal_id)
        att = Attachment(self, term, term.pty.ring.raw(), self._queue_bytes)
        if term.exited:
            att._finish(term.exit_code)
        term.subscribers.add(att)
        return att

    def _unsubscribe(self, term: _Terminal, att: Attachment) -> None:
        if att in term.subscribers:
            term.subscribers.discard(att)
            if not term.subscribers:
                term.idle_since = self._clock()

    def _on_output(self, term: _Terminal, data: bytes) -> None:
        for att in list(term.subscribers):
            if not att._feed(data):
                log.warning(
                    "terminal %s: a subscriber fell behind and was dropped", term.terminal_id
                )
                self._unsubscribe(term, att)

    def _on_exit(self, term: _Terminal, code: int) -> None:
        term.exited = True
        term.exit_code = code
        for att in term.subscribers:
            att._finish(code)

    # ------------------------------------------------------------------ close

    async def close(
        self, session_id: str, terminal_id: str, grace: float = CLOSE_GRACE_SECONDS
    ) -> bool:
        """Close a terminal and kill its whole session; idempotent. False when it was not
        registered (unknown, or already closed or closing)."""
        term = self._terminals.get(terminal_id)
        if term is None or not term.started or term.session_id != session_id:
            return False
        self._unregister([term])
        await self._terminate(term, grace)
        return True

    async def close_session(self, session_id: str, grace: float = CLOSE_GRACE_SECONDS) -> None:
        """Close every terminal of a session (its stop or destroy)."""
        victims = [t for t in self._terminals.values() if t.session_id == session_id]
        self._unregister(victims)
        await self._terminate_all(victims, grace)

    async def reap(self) -> list[str]:
        """One reaper tick: close terminals with no client that exited or sat idle (since
        creation, or the last detach) for `terminal_idle_reap_seconds`. Returns their ids."""
        now = self._clock()
        limit = self.limits.terminal_idle_reap_seconds
        victims = [
            t
            for t in self._terminals.values()
            if t.started and not t.subscribers and (t.exited or now - t.idle_since >= limit)
        ]
        self._unregister(victims)
        for term in victims:
            log.info("session %s: terminal %s reaped", term.session_id, term.terminal_id)
        await self._terminate_all(victims, CLOSE_GRACE_SECONDS)
        return [t.terminal_id for t in victims]

    async def shutdown(self, grace: float = CLOSE_GRACE_SECONDS) -> None:
        """Stop the reaper and close every terminal; no new terminal or attach after this."""
        self._closed = True
        if self._reaper is not None:
            self._reaper.cancel()
            await asyncio.gather(self._reaper, return_exceptions=True)
            self._reaper = None
        victims = list(self._terminals.values())
        self._unregister(victims)
        await self._terminate_all(victims, grace)

    def _unregister(self, victims: Sequence[_Terminal]) -> None:
        for term in victims:
            if self._terminals.get(term.terminal_id) is term:
                del self._terminals[term.terminal_id]

    async def _terminate_all(self, victims: Sequence[_Terminal], grace: float) -> None:
        results = await asyncio.gather(
            *(self._terminate(t, grace) for t in victims), return_exceptions=True
        )
        for term, result in zip(victims, results, strict=True):
            if isinstance(result, BaseException):
                log.error("terminal %s: close failed", term.terminal_id, exc_info=result)

    async def _terminate(self, term: _Terminal, grace: float) -> None:
        """SIGTERM the shell's session, close the PTY (SIGKILL after `grace`), then SIGKILL
        whatever is left of the session; every subscriber gets the exit marker."""
        sid = term.pty.pid
        if sid is not None:
            _signal_all(_session_members(sid), signal.SIGTERM)
        try:
            await term.pty.close(grace)
            if sid is not None:
                await self._kill_session(sid)
        finally:
            code = term.exit_code if term.exited else term.pty.returncode
            term.exited = True
            term.exit_code = code
            for att in list(term.subscribers):
                att._finish(code)

    @staticmethod
    async def _kill_session(sid: int) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SESSION_KILL_SECONDS
        while members := _session_members(sid):
            _signal_all(members, signal.SIGKILL)
            if loop.time() >= deadline:
                log.warning("terminal session %s: processes %s survived SIGKILL", sid, members)
                return
            await asyncio.sleep(0.02)

    # ------------------------------------------------------------------ reaper

    def _ensure_reaper(self) -> None:
        if self._reaper is None and not self._closed:
            self._reaper = asyncio.ensure_future(self._reap_loop())

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(self._reap_interval)
            try:
                await self.reap()
            except Exception:
                log.exception("terminal reaper failed")
