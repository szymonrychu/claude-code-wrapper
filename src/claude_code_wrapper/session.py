"""One claude session's process lifecycle: spawn, boot handshake, readiness, relaunch, stop.

The child is interactive claude under a PTY (D1), in its own workdir with its own
`CLAUDE_CONFIG_DIR` (D4) and an env built from scratch so no service secret reaches it (D9).
Boot follows the tatara/magic method (D13): answer each configured dialog once per boot, then
call the session ready on a ready marker or output quiescence, never before `min_boot_seconds`
(a submit while the TUI is still painting exits claude). A boot that never gets there is
relaunched once; a crash after boot is relaunched within `max_restarts`, with `--resume` only
when the transcript exists (resuming a conversation that was never written exits claude at once
and burns the budget). The ring is emptied before every relaunch so the old screen cannot
satisfy the new boot's dialog or marker checks.

Turns (D13): one at a time. `submit` reserves the slot and persists the record before the first
byte is written, then pastes the prompt (with a `<!-- ccw turn: <id> -->` marker, because Stop
payloads carry no turn id) and sends CR as a separate write. A turn ends exactly once, on the
first of: a matching Stop (`complete`), an interrupt (resolved by the transcript interrupt line or
a second ESC after `interrupt_settle_seconds`, since claude runs no Stop hook for an interrupted
turn), a fatal pattern on screen, claude exiting, the hard `turn_max_seconds` limit, or `stop`.
Every end frees the slot, persists the record and calls `on_turn_end`. A quiet turn is only
reported (`on_stall`), never failed: silence is not a stall.

The screen is never the source of turn results; it is read only for boot, fatal patterns and
debug tails, and every tail that leaves this module is redacted.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import inspect
import logging
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .claude_env import SECRET_KEY_RE, ClaudeEnv
from .config import BootConfig, BootDialog, LimitsConfig, SessionsConfig
from .pty_proc import PtyClosed, PtyProcess
from .redact import Redactor
from .ring import Ring, _squash, strip_ansi
from .turns import (
    Origin,
    Probe,
    TurnRecord,
    TurnState,
    merged_annotations,
    new_probe_id,
    new_turn_id,
    now_iso,
)

log = logging.getLogger(__name__)

#: How often the boot wait re-reads the screen.
BOOT_POLL_SECONDS = 0.15
#: Pause between the keys of one dialog answer (Down then CR must land as separate reads).
KEY_GAP_SECONDS = 0.25
#: Characters of screen kept in boot errors and fatal reports.
TAIL_CHARS = 800
#: Grace for closing a claude that already exited (reaps leftover group members).
DEAD_CLOSE_GRACE = 1.0
#: The pinned transcript project dir (`CLAUDE_CODE_PROJECT_DIR_NAME`).
PROJECT_DIR_NAME = "workdir"
DANGEROUS_FLAG = "--dangerously-skip-permissions"
#: The only service env vars every child inherits.
BASE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "USER", "SHELL", "TMPDIR")
GATE_FAIL_MODES = ("allow", "deny")
ESC = b"\x1b"
DEFAULT_PROBE_TEXT = "Reply with CCW-ALIVE and one sentence on what you are doing, then continue."
#: The turn watchdog re-checks at least this often, so a cleared stall re-arms promptly.
TURN_WATCH_MAX_SLEEP = 0.25
#: `note_activity` appends `last_activity_at` to turns.jsonl at most this often (it runs on every
#: event); the store serves every update at once.
ACTIVITY_PERSIST_SECONDS = 5.0
#: Probes remembered for `get_probe`; the oldest are forgotten first.
MAX_PROBES = 100

STATES = ("booting", "ready", "busy", "stopping", "stopped", "dead")
PUBLIC_STATES = ("creating", "ready", "busy", "stopped", "failed", "dead")
_PUBLIC = {"booting": "creating", "stopping": "stopped"}

# A complete escape sequence starting at an ESC: CSI, terminated OSC, or a short escape whose
# final byte is not "[" or "]" (those open a CSI or OSC that is still incomplete).
_COMPLETE_ESC = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[ -/]*[0-Z\\^-~])"
)
#: An unterminated escape longer than this is not held back any more.
_MAX_ESC_HOLD = 256


def public_state(state: str) -> str:
    """The API vocabulary for an internal state (booting -> creating, stopping -> stopped)."""
    if state not in STATES:
        raise ValueError(f"unknown session state {state!r}")
    return _PUBLIC.get(state, state)


class TurnRejected(Exception):
    """A turn or probe cannot be written now. `reason`: busy, booting or dead."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BootError(Exception):
    """claude did not reach its prompt. `code`: boot_timeout, boot_exited, spawn_failed, stopped."""

    def __init__(self, code: str, tail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.tail = tail


@dataclass
class SessionSpec:
    """Everything needed to launch one session's claude; `env` is the complete child env."""

    session_id: str
    claude_session_id: str
    workdir: str
    config_dir: str
    session_dir: str
    binary: str
    model: str
    effort: str
    extra_args: list[str]
    env: dict[str, str]
    boot: BootConfig = field(default_factory=BootConfig)
    max_restarts: int = 3
    resume: bool = False
    #: Turn behaviour: `turn_marker`, `turn_stall_seconds`, `interrupt_settle_seconds`.
    sessions: SessionsConfig = field(default_factory=SessionsConfig)
    #: Turn limits: `turn_max_seconds`.
    limits: LimitsConfig = field(default_factory=LimitsConfig)


def transcript_path_for(spec: SessionSpec) -> str:
    """`<config_dir>/projects/<project dir name>/<claude_session_id>.jsonl`."""
    name = spec.env.get("CLAUDE_CODE_PROJECT_DIR_NAME") or PROJECT_DIR_NAME
    return os.path.join(spec.config_dir, "projects", name, f"{spec.claude_session_id}.jsonl")


def _argv(spec: SessionSpec, *, resume: bool) -> list[str]:
    if any(a.split("=", 1)[0] == DANGEROUS_FLAG for a in spec.extra_args):
        raise ValueError(f"{DANGEROUS_FLAG} is not allowed; use claude.permission_mode")
    argv = [spec.binary]
    if spec.model:
        argv += ["--model", spec.model]
    if spec.effort:
        argv += ["--effort", spec.effort]
    argv += ["--resume" if resume else "--session-id", spec.claude_session_id]
    return argv + list(spec.extra_args)


def build_argv(spec: SessionSpec) -> list[str]:
    """claude's argv: `--resume` when asked or when a transcript exists, else `--session-id`."""
    return _argv(spec, resume=spec.resume or os.path.isfile(transcript_path_for(spec)))


def build_env(
    *,
    service_env: Mapping[str, str],
    claude_env: ClaudeEnv,
    config_dir: str,
    gitconfig: str,
    session_id: str,
    internal_url: str,
    hook_token: str,
    gate_fail: str,
    gate_timeout: float,
    mise_env: Mapping[str, str] | None,
    passthrough: Iterable[str],
) -> dict[str, str]:
    """The complete claude child env, built from an allow-list rather than filtered.

    Layering, later wins: the base vars, configured passthrough names, `TERM`,
    `claude_env`, `mise_env`, then the service-managed vars. Passthrough never carries a
    `CCW_*` name or a managed name, so a listed `CCW_AUTH_TOKEN` still stays out.
    """
    if gate_fail not in GATE_FAIL_MODES:
        raise ValueError(
            f"gate_fail must be one of {', '.join(GATE_FAIL_MODES)}, got {gate_fail!r}"
        )
    managed = {
        "CLAUDE_CONFIG_DIR": config_dir,
        "CLAUDE_CODE_PROJECT_DIR_NAME": PROJECT_DIR_NAME,
        "GIT_CONFIG_GLOBAL": gitconfig,
        "CCW_SESSION_ID": session_id,
        "CCW_INTERNAL_URL": internal_url,
        "CCW_HOOK_TOKEN": hook_token,
        "CCW_TOOL_GATE_FAIL": gate_fail,
        "CCW_GATE_TIMEOUT": f"{gate_timeout:g}",
    }
    env = {name: service_env[name] for name in BASE_ENV if name in service_env}
    for name in passthrough:
        if name.startswith("CCW_") or name in managed:
            log.warning("passthrough of %s refused: service-managed or service-private", name)
        elif name in service_env:
            env[name] = service_env[name]
    env["TERM"] = "xterm-256color"
    env.update(claude_env.to_env())
    env.update(mise_env or {})
    env.update(managed)
    return env


class SessionListener:
    """Receives a session's lifecycle callbacks. Every method is a no-op by default.

    A callback may be a coroutine function; the session schedules it and does not wait.
    `on_state` receives the public state vocabulary (`PUBLIC_STATES`).
    """

    def bind(self, session: Session) -> None:
        """Called by the `Session` constructor with the session it listens to."""

    def on_state(self, state: str) -> None:
        """The public state changed."""

    def on_output(self, text_chunk: str) -> None:
        """Decoded PTY output, escapes included."""

    def on_fatal(self, text: str, tail: str) -> None:
        """A configured fatal pattern appeared; `tail` is the redacted screen tail."""

    def on_restart(self, count: int, resumed: bool) -> None:
        """claude crashed and is back at its prompt after relaunch number `count`."""

    def on_exit(self, code: int) -> None:
        """claude exited unexpectedly and will not be relaunched; the session is dead."""

    def on_stall(self, turn_id: str) -> None:
        """The running turn has been quiet for `turn_stall_seconds`."""

    def on_turn_end(self, record: Any) -> None:
        """A turn reached a terminal state."""


class FatalScanner:
    """Finds fatal patterns in a stream of output chunks, each occurrence exactly once.

    Matching ignores escapes, whitespace and case (the TUI positions words with cursor moves,
    not spaces). A carry of the previous chunk's tail lets a pattern split across chunks match,
    and an escape sequence cut at a chunk end is held back until it completes.
    """

    def __init__(self, patterns: Iterable[str]) -> None:
        self._patterns = [(p, needle) for p in patterns if (needle := _squash(p))]
        self._keep = max((len(n) for _, n in self._patterns), default=1) - 1
        self.reset()

    def reset(self) -> None:
        self._carry = ""
        self._hold = ""

    def feed(self, text: str) -> list[str]:
        """The patterns (as configured) whose occurrences end inside `text`, in screen order."""
        raw, self._hold = _split_partial_escape(self._hold + text)
        window = self._carry + _squash(strip_ansi(raw))
        hits: list[tuple[int, str]] = []
        for pattern, needle in self._patterns:
            pos = window.find(needle, max(0, len(self._carry) - len(needle) + 1))
            while pos >= 0:
                hits.append((pos, pattern))
                pos = window.find(needle, pos + len(needle))
        self._carry = window[-self._keep :] if self._keep else ""
        return [pattern for _, pattern in sorted(hits)]


def _split_partial_escape(text: str) -> tuple[str, str]:
    i = text.rfind("\x1b")
    if i < 0 or len(text) - i > _MAX_ESC_HOLD or _COMPLETE_ESC.match(text, i):
        return text, ""
    return text[:i], text[i:]


def _redactor_for(env: Mapping[str, str]) -> Redactor:
    return Redactor(value for name, value in env.items() if SECRET_KEY_RE.search(name))


@dataclass
class _PendingInterrupt:
    """An interrupt in flight for `record`; `final` is the state the turn ends in."""

    record: TurnRecord
    final: TurnState
    done: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Future[None] | None = None


class Session:
    """One claude process (relaunched as needed), its boot state machine and its turn slot.

    `state` is internal (`STATES`); `snapshot()["state"]` and `on_state` use `PUBLIC_STATES`.
    `pty` is the current `PtyProcess`, `ring` the screen shared across relaunches, and
    `transcript_path` the conversation file (the router may replace it with the path a hook
    payload reports). `turn_store` (a `TurnStore`) receives every change of every turn record.
    """

    def __init__(
        self,
        spec: SessionSpec,
        listener: SessionListener,
        *,
        turn_store: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.spec = spec
        self.listener = listener
        self.turn_store = turn_store
        self.clock = clock
        self.state = "booting"
        self.ring = Ring()
        self.pty: PtyProcess | None = None
        self.transcript_path = transcript_path_for(spec)
        self.restarts = 0
        self.last_fatal: str | None = None
        self.started_at: str | None = None
        self.ready_at: str | None = None
        self.boot_error: BootError | None = None
        self._redactor = _redactor_for(spec.env)
        self._scanner = FatalScanner(spec.boot.fatal_patterns)
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._exited = asyncio.Event()
        self._public: str | None = None
        self._stopping = False
        self._recovery: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Future[Any]] = set()
        self._turn: TurnRecord | None = None
        self._turn_watch: asyncio.Future[None] | None = None
        self._interrupting: _PendingInterrupt | None = None
        self._last_activity = clock()
        self._activity_persisted = self._last_activity
        self._probes: dict[str, Probe] = {}
        listener.bind(self)

    # -- state and reporting ---------------------------------------------------------------

    def _set_state(self, state: str) -> None:
        public = public_state(state)
        self.state = state
        if public != self._public:
            self._public = public
            self._notify("on_state", public)

    def snapshot(self) -> dict[str, Any]:
        pty = self.pty
        return {
            "state": public_state(self.state),
            "pid": pty.pid if pty is not None and pty.alive else None,
            "restarts": self.restarts,
            "last_fatal": self.last_fatal,
            "started_at": self.started_at,
            "ready_at": self.ready_at,
        }

    def tail(self, n: int = TAIL_CHARS) -> str:
        """The last `n` characters of the screen, redacted (redacted before the cut)."""
        return self._redactor.redact(self.ring.text())[-n:] if n > 0 else ""

    def _notify(self, name: str, *args: Any) -> None:
        try:
            result = getattr(self.listener, name)(*args)
        except Exception:
            log.exception("session %s: listener %s failed", self.spec.session_id, name)
            return
        if inspect.isawaitable(result):
            self._track(asyncio.ensure_future(result))

    def _track(self, task: asyncio.Future[Any]) -> asyncio.Future[Any]:
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Future[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error(
                "session %s: background task failed",
                self.spec.session_id,
                exc_info=task.exception(),
            )

    # -- launch and boot -------------------------------------------------------------------

    async def start(self) -> None:
        """Launch claude and return once it is idle at its prompt; raises `BootError`."""
        if self.started_at is not None:
            raise RuntimeError("Session.start() may be called only once")
        self.started_at = now_iso()
        self._set_state("booting")
        await self._boot(resume=self.spec.resume or os.path.isfile(self.transcript_path))

    async def _boot(self, *, resume: bool) -> bool:
        """Launch and wait for ready, relaunching once; returns whether the launch resumed.

        The first launch resumes when `resume`; the relaunch only when the transcript
        exists. Two failed boots in a row leave the session dead and raise `BootError`.
        """
        code, tail = "", ""
        for attempt in range(2):
            if self._stopping:
                raise BootError("stopped")
            if attempt:
                log.warning(
                    "session %s: claude did not boot (%s), relaunching once",
                    self.spec.session_id,
                    code,
                )
                self.ring.reset()
                resume = os.path.isfile(self.transcript_path)
            try:
                await self._spawn(_argv(self.spec, resume=resume))
            except OSError as exc:
                self._set_state("dead")
                self.boot_error = BootError("spawn_failed", self._redactor.redact(str(exc)))
                raise self.boot_error from exc
            assert self.pty is not None
            code = await self._boot_wait(self.pty)
            if code == "ready":
                self.ready_at = now_iso()
                self._set_state("ready")
                log.info("session %s: claude ready pid=%s", self.spec.session_id, self.pty.pid)
                return resume
            if code == "stopped":
                raise BootError("stopped")
            tail = self.tail()
            await self.pty.close(DEAD_CLOSE_GRACE)
        if self._stopping:
            raise BootError("stopped")
        log.error(
            "session %s: claude never reached its prompt (%s) tail=%r",
            self.spec.session_id,
            code,
            tail,
        )
        self._set_state("dead")
        self.boot_error = BootError(code, tail)
        raise self.boot_error

    async def _spawn(self, argv: list[str]) -> None:
        pty = PtyProcess()
        pty.ring = self.ring  # one screen across relaunches; reset() empties it in between
        self._scanner.reset()
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._exited = asyncio.Event()
        self.pty = pty
        log.info("session %s: launching %s", self.spec.session_id, argv)
        await pty.start(
            argv,
            self.spec.workdir,
            self.spec.env,
            on_output=lambda data: self._on_output(pty, data),
            on_exit=lambda code: self._on_exit(pty, code),
        )

    async def _boot_wait(self, pty: PtyProcess) -> str:
        """`ready`, or why not: `boot_timeout`, `boot_exited` or `stopped`."""
        boot = self.spec.boot
        start = self.clock()
        deadline = start + boot.timeout_seconds
        last_written, last_change = self.ring.written, start
        answered: set[int] = set()
        while True:
            if self._stopping:
                return "stopped"
            if self._exited.is_set():
                return "boot_exited"
            now = self.clock()
            if now >= deadline:
                return "boot_timeout"
            pending = [
                i
                for i, dialog in enumerate(boot.dialogs)
                if i not in answered and self.ring.contains(dialog.pattern)
            ]
            if pending:
                answered.add(pending[0])
                await self._answer(pty, boot.dialogs[pending[0]])
                last_written, last_change = self.ring.written, self.clock()
                continue
            written = self.ring.written
            if written != last_written:
                last_written, last_change = written, now
            marker = any(self.ring.contains(m) for m in boot.ready_markers)
            quiet = now - last_change >= boot.idle_ready_seconds
            if (marker or quiet) and now - start >= boot.min_boot_seconds:
                return "ready"
            await asyncio.sleep(BOOT_POLL_SECONDS)

    async def _answer(self, pty: PtyProcess, dialog: BootDialog) -> None:
        log.info("session %s: answering boot dialog %r", self.spec.session_id, dialog.pattern)
        await asyncio.sleep(dialog.delay_seconds)
        for i, key in enumerate(dialog.keys):
            if i:
                await asyncio.sleep(KEY_GAP_SECONDS)
            if self._stopping:
                return
            try:
                await pty.write(key.encode())
            except PtyClosed:
                return

    # -- output and exit -------------------------------------------------------------------

    def _on_output(self, pty: PtyProcess, data: bytes) -> None:
        if pty is not self.pty:
            return
        text = self._decoder.decode(data)
        if not text:
            return
        self._notify("on_output", text)
        for pattern in self._scanner.feed(text):
            self.last_fatal = pattern
            log.warning("session %s: fatal pattern %r on screen", self.spec.session_id, pattern)
            self._notify("on_fatal", pattern, self.tail())
            record = self._turn
            # claude sits at its prompt after a fatal error: fail the turn, or the slot wedges.
            # A prompt that quotes the pattern is echoed on screen; that is not the error.
            if record is not None and _squash(pattern) not in _squash(record.text):
                self._end_turn(record, "failed", error=pattern)

    def _on_exit(self, pty: PtyProcess, code: int) -> None:
        if pty is not self.pty:
            return
        self._exited.set()  # a booting claude: the boot wait sees this
        if self._stopping or self.state not in ("ready", "busy"):
            return
        record = self._turn
        if record is not None:  # never re-pasted: the client decides whether to retry
            self._end_turn(record, "failed", error="claude_exited", next_state=None)
        log.warning(
            "session %s: claude exited unexpectedly code=%s tail=%r",
            self.spec.session_id,
            code,
            self.tail(),
        )
        # Leave ready/busy at once, so nothing is written to the dead claude meanwhile. A
        # relaunch starts from an empty screen; a dead session keeps its last one for debugging.
        if self.restarts < self.spec.max_restarts:
            self.ring.reset()
            self._set_state("booting")
        else:
            self._set_state("dead")
        self._recovery = asyncio.ensure_future(self._recover(pty, code))
        self._track(self._recovery)

    async def _recover(self, old: PtyProcess, code: int) -> None:
        """claude exited on its own after boot: relaunch within the budget, else dead."""
        await old.close(DEAD_CLOSE_GRACE)
        if self._stopping:
            return
        if self.restarts >= self.spec.max_restarts:
            log.error(
                "session %s: restart budget (%d) exhausted",
                self.spec.session_id,
                self.spec.max_restarts,
            )
            self._set_state("dead")
            self._notify("on_exit", code)
            return
        self.restarts += 1
        self.ring.reset()
        self._set_state("booting")
        try:
            resumed = await self._boot(resume=os.path.isfile(self.transcript_path))
        except BootError as exc:
            if exc.code != "stopped":
                self._notify("on_exit", code)
            return
        self._notify("on_restart", self.restarts, resumed)

    # -- stop ------------------------------------------------------------------------------

    async def stop(self, grace: float = 5.0) -> None:
        """Terminate claude (SIGTERM, then SIGKILL after `grace`); nothing relaunches after."""
        if self.state == "stopped":
            return
        self._stopping = True
        record = self._turn
        if record is not None:
            self._end_turn(record, "failed", error="session_stopped", next_state=None)
        self._set_state("stopping")
        if self.pty is not None:
            await self.pty.close(grace)
        recovery = self._recovery
        if recovery is not None and recovery is not asyncio.current_task():
            with contextlib.suppress(Exception):
                await recovery
        if self.pty is not None:  # a relaunch that raced the stop is closed too
            await self.pty.close(grace)
        self._set_state("stopped")

    # -- turns -----------------------------------------------------------------------------

    @property
    def current_turn(self) -> TurnRecord | None:
        """The running turn's live record, if any."""
        return self._turn

    def _writable_pty(self) -> PtyProcess:
        """The PTY a turn or probe may be written to, else `TurnRejected(booting|dead)`."""
        if self.state == "booting":
            raise TurnRejected("booting")
        if self.state not in ("ready", "busy") or self.pty is None:
            raise TurnRejected("dead")
        return self.pty

    def check_submittable(self) -> PtyProcess:
        """Raise `TurnRejected(booting|dead|busy)` if a turn cannot start now (no side effects)."""
        pty = self._writable_pty()
        if self.state == "busy" or self._turn is not None:
            raise TurnRejected("busy")
        return pty

    async def submit(
        self,
        text: str,
        *,
        origin: Origin = "client",
        callback_url: str | None = None,
        turn_id: str | None = None,
        annotations: dict[str, Any] | None = None,
    ) -> TurnRecord:
        """Start a turn: reserve the slot, persist the record, then paste and CR.

        `turn_id` is an id allocated earlier (the router names it in the onTurn payload).
        Raises `TurnRejected` (booting, dead, busy). A claude that exits under the write fails
        the turn with `claude_exited`; the record is returned either way.
        """
        pty = self.check_submittable()
        now = now_iso()
        record = TurnRecord(
            turn_id=turn_id or new_turn_id(),
            session_id=self.spec.session_id,
            origin=origin,
            state="running",
            text=text,
            callback_url=callback_url,
            started_at=now,
            last_activity_at=now,
            annotations=dict(annotations or {}),
        )
        # Reserve before the first byte: a second submit racing this one sees busy.
        self._turn = record
        self._last_activity = self._activity_persisted = self.clock()
        self._set_state("busy")
        self._persist(record)
        self._arm_watch(record)
        prompt = text
        if self.spec.sessions.turn_marker:
            prompt += f"\n\n<!-- ccw turn: {record.turn_id} -->"
        log.info("session %s: turn %s submitted (%s)", self.spec.session_id, record.turn_id, origin)
        try:
            await pty.send_prompt(prompt, self.spec.boot.submit_delay_seconds)
        except PtyClosed:
            # A claude that died is handled by the exit path (relaunch or dead); a live one
            # whose write failed gets its slot back here.
            self._end_turn(
                record, "failed", error="claude_exited", next_state="ready" if pty.alive else None
            )
        return record

    def annotate(self, record: TurnRecord, annotations: dict[str, Any]) -> bool:
        """Shallow-merge hook annotations into `record` and persist it.

        False (nothing changed) when the merge would exceed the 16 KiB cap.
        """
        merged = merged_annotations(record.annotations, annotations)
        if merged is None:
            return False
        record.annotations = merged
        self._persist(record)
        return True

    def _arm_watch(self, record: TurnRecord) -> None:
        if self.spec.sessions.turn_stall_seconds > 0 or self.spec.limits.turn_max_seconds > 0:
            self._turn_watch = self._track(asyncio.ensure_future(self._watch_turn(record)))

    async def _watch_turn(self, record: TurnRecord) -> None:
        """Report a quiet turn once per quiet period; end it as `timeout` at the hard limit."""
        stall = self.spec.sessions.turn_stall_seconds
        limit = self.spec.limits.turn_max_seconds
        started = self.clock()
        while self._turn is record:
            now = self.clock()
            if limit > 0 and now - started >= limit:
                log.warning(
                    "session %s: turn %s hit turn_max_seconds=%g, interrupting",
                    self.spec.session_id,
                    record.turn_id,
                    limit,
                )
                await self._interrupt(record, "timeout")
                return
            wait = TURN_WATCH_MAX_SLEEP
            if limit > 0:
                wait = min(wait, started + limit - now)
            if stall > 0 and record.stall_suspected_since is None:
                quiet = now - self._last_activity
                if quiet >= stall:
                    self._suspect_stall(record)
                else:
                    wait = min(wait, stall - quiet)
            await asyncio.sleep(max(wait, 0.0))

    def _suspect_stall(self, record: TurnRecord) -> None:
        record.stall_suspected_since = now_iso()
        log.warning(
            "session %s: turn %s quiet for %gs (stall suspected, not failed)",
            self.spec.session_id,
            record.turn_id,
            self.spec.sessions.turn_stall_seconds,
        )
        self._persist(record)
        self._notify("on_stall", record.turn_id)

    def note_activity(self) -> None:
        """Something happened (a hook, a transcript line): reset the stall timer."""
        now = self.clock()
        self._last_activity = now
        record = self._turn
        if record is None:
            return
        record.last_activity_at = now_iso()
        if record.stall_suspected_since is not None:
            record.stall_suspected_since = None  # re-armed: a new quiet period reports again
        elif now - self._activity_persisted < ACTIVITY_PERSIST_SECONDS:
            # Served at once; appended to turns.jsonl at most once per ACTIVITY_PERSIST_SECONDS.
            self._persist(record, append=False)
            return
        self._activity_persisted = now
        self._persist(record)

    def record_tool_call(self) -> None:
        """Count one finished tool call on the running turn (no-op when idle)."""
        record = self._turn
        if record is None:
            return
        record.tool_calls += 1
        self._persist(record)

    def stop_target(
        self, payload: Mapping[str, Any], marker_turn_id: str | None = None
    ) -> TurnRecord | None:
        """The record a Stop hook payload applies to, or None when it is stale.

        A Stop for another claude session, a booting or dead session, or (while busy) carrying
        the marker of another turn targets nothing. While busy it targets the running turn
        (`current_turn`). A Stop while ready with no turn running is a background completion
        (Workflow, Monitor) and targets a new, not yet persisted `origin="agent"` record.
        """
        if payload.get("session_id") != self.spec.claude_session_id:
            log.info(
                "session %s: ignoring Stop for claude session %r",
                self.spec.session_id,
                payload.get("session_id"),
            )
            return None
        record = self._turn
        if self.state == "busy" and record is not None:
            if marker_turn_id is not None and marker_turn_id != record.turn_id:
                log.info(
                    "session %s: ignoring stale Stop of %s while %s runs",
                    self.spec.session_id,
                    marker_turn_id,
                    record.turn_id,
                )
                return None
            return record
        if self.state == "ready" and record is None:
            now = now_iso()
            return TurnRecord(
                turn_id=new_turn_id(),
                session_id=self.spec.session_id,
                origin="agent",
                started_at=now,
                last_activity_at=now,
            )
        return None

    def complete(
        self,
        payload: Mapping[str, Any],
        *,
        usage: dict[str, Any] | None = None,
        model_tokens: dict[str, Any] | None = None,
        marker_turn_id: str | None = None,
    ) -> TurnRecord | None:
        """Apply a Stop hook payload to its `stop_target`; the completed record, or None."""
        record = self.stop_target(payload, marker_turn_id)
        if record is None:
            return None
        message = payload.get("last_assistant_message")
        record.final_text = message if isinstance(message, str) else None
        record.stop_reason = "end_turn"
        record.usage = usage
        record.model_tokens = model_tokens
        self._end_turn(record, "completed")
        return record

    def _end_turn(
        self,
        record: TurnRecord,
        state: TurnState,
        *,
        error: str | None = None,
        next_state: str | None = "ready",
    ) -> bool:
        """The single terminal transition: free the slot, persist, notify. Once per turn."""
        if record.terminal:
            return False
        if self._turn is record:
            self._turn = None
            watch, self._turn_watch = self._turn_watch, None
            if watch is not None and watch is not asyncio.current_task():
                watch.cancel()
        record.state = state
        record.error = error
        record.completed_at = now_iso()
        pending = self._interrupting
        if pending is not None and pending.record is record:
            pending.done.set()
        if next_state is not None and self.state == "busy":
            self._set_state(next_state)
        log.info(
            "session %s: turn %s %s%s",
            self.spec.session_id,
            record.turn_id,
            state,
            f" ({error})" if error else "",
        )
        self._persist(record)
        self._notify("on_turn_end", record)
        return True

    def _persist(self, record: TurnRecord, *, append: bool = True) -> None:
        if self.turn_store is None:
            return
        try:
            if append:
                self.turn_store.put(record)
            else:
                self.turn_store.refresh(record)
        except Exception:
            log.exception("session %s: persisting turn %s failed", self.spec.session_id, record)

    # -- interrupt -------------------------------------------------------------------------

    async def interrupt(self) -> str | None:
        """Interrupt the running turn; its id once it has ended, None when idle.

        One ESC, then up to `interrupt_settle_seconds` for the transcript interrupt line
        (`on_interrupted`); without it a second ESC. claude runs no Stop hook for an
        interrupted turn, so the turn is ended here either way and the slot freed.
        """
        record = self._turn
        if record is None or self.state != "busy":
            return None
        return await self._interrupt(record, "interrupted")

    async def _interrupt(self, record: TurnRecord, final: TurnState) -> str:
        pending = self._interrupting
        if pending is None or pending.record is not record:
            pending = _PendingInterrupt(record, final)
            self._interrupting = pending
            pending.task = self._track(asyncio.ensure_future(self._run_interrupt(pending)))
        assert pending.task is not None
        # Shielded: a caller that goes away (a dropped request) cannot strand the slot.
        await asyncio.shield(pending.task)
        return record.turn_id

    async def _run_interrupt(self, pending: _PendingInterrupt) -> None:
        record = pending.record
        try:
            await self._send_key(ESC)
            try:
                await asyncio.wait_for(
                    pending.done.wait(), self.spec.sessions.interrupt_settle_seconds
                )
            except TimeoutError:
                if not record.terminal:
                    log.warning(
                        "session %s: no interrupt line for %s after %gs, second ESC",
                        self.spec.session_id,
                        record.turn_id,
                        self.spec.sessions.interrupt_settle_seconds,
                    )
                    await self._send_key(ESC)
            self._end_turn(record, pending.final)
        finally:
            if self._interrupting is pending:
                self._interrupting = None

    async def _send_key(self, key: bytes) -> None:
        pty = self.pty
        if pty is None:
            return
        with contextlib.suppress(PtyClosed):  # the exit path ends the turn
            await pty.write(key)

    def on_interrupted(self) -> None:
        """The transcript shows an interrupt: end the running turn (pending or unprompted)."""
        record = self._turn
        if record is None:
            return
        pending = self._interrupting
        final: TurnState = "interrupted"
        if pending is not None and pending.record is record:
            final = pending.final
        self._end_turn(record, final)

    # -- probes ----------------------------------------------------------------------------

    async def probe(self, text: str | None = None) -> Probe:
        """Write a liveness probe without taking the slot (ready or busy).

        The probe is pending until the router sees its marker in the transcript
        (`probe_delivered`) and answered once assistant text with `CCW-ALIVE` follows
        (`probe_answered`). Raises `TurnRejected` (booting, dead).
        """
        pty = self._writable_pty()
        probe = Probe(
            probe_id=new_probe_id(), text=text or DEFAULT_PROBE_TEXT, created_at=now_iso()
        )
        self._probes[probe.probe_id] = probe
        while len(self._probes) > MAX_PROBES:
            del self._probes[next(iter(self._probes))]
        prompt = f"{probe.text}\n\n<!-- ccw probe: {probe.probe_id} -->"
        try:
            await pty.send_prompt(prompt, self.spec.boot.submit_delay_seconds)
        except PtyClosed as exc:
            self._probes.pop(probe.probe_id, None)
            raise TurnRejected("dead") from exc
        log.info("session %s: probe %s written", self.spec.session_id, probe.probe_id)
        return probe

    def get_probe(self, probe_id: str) -> Probe | None:
        return self._probes.get(probe_id)

    def probe_delivered(self, probe_id: str) -> Probe | None:
        """The probe's marker reached the transcript; the probe, or None when unknown."""
        probe = self._probes.get(probe_id)
        if probe is not None and probe.state == "pending":
            probe.state = "delivered"
            probe.delivered_at = now_iso()
        return probe

    def probe_answered(self) -> list[Probe]:
        """Assistant text with `CCW-ALIVE` arrived: every delivered probe is answered."""
        answered = [p for p in self._probes.values() if p.state == "delivered"]
        now = now_iso()
        for probe in answered:
            probe.state = "answered"
            probe.answered_at = now
        return answered
