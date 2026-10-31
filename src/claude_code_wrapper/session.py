"""One claude session's process lifecycle: spawn, boot handshake, readiness, relaunch, stop.

The child is interactive claude under a PTY (D1), in its own workdir with its own
`CLAUDE_CONFIG_DIR` (D4) and an env built from scratch so no service secret reaches it (D9).
Boot follows the tatara/magic method (D13): answer each configured dialog once per boot, then
call the session ready on a ready marker or output quiescence, never before `min_boot_seconds`
(a submit while the TUI is still painting exits claude). A boot that never gets there is
relaunched once; a crash after boot is relaunched within `max_restarts`, with `--resume` only
when the transcript exists (resuming a conversation that was never written exits claude at once
and burns the budget). `restart_for_dialog` takes the same relaunch path for a dialog a slash
command left open (design D7): it spends no `max_restarts`, and the slot (the command run, or the
turn whose commands run) is retaken busy once claude is back, so nothing is typed into the dialog
and nothing else takes the session in between. With `sessions.restart_continue_prompt` set, a
turn running when claude crashes is held (still `running`) across the relaunch; a resumed
relaunch is sent the continue prompt with the same turn marker and the turn goes on
(`on_turn_resumed`). The ring is emptied before every relaunch so the old screen cannot satisfy
the new boot's dialog or marker checks.
`continue_turn` is that paste for any caller: the router also uses it to resume a turn after the
slash commands it ran at the turn's Stop (`hold_for_commands` marks that command phase, in which
claude sits at its prompt with no turn of its own: an interrupt or the time limit ends the turn at
once, and a transcript interrupt line belongs to the command, not to the turn). A slash command
claude runs as a model turn (a prompt command, design D8) becomes a turn with no paste of its own:
`adopt_turn` turns an idle session's command slot into a running turn, and
`release_commands_hold` ends the command phase of a held turn that continues as that model turn.

Service shutdown (D5): `stop(cause="shutdown")` does not end the running turn; it stays `running`
in turns.jsonl marked `interrupted="shutdown"`. A session resumed later is built with that record
(`resume_turn`, also a turn a killed process left `running`) and holds it from the first boot;
once claude is ready it is continued with the continue prompt and the same marker
(`on_turn_resumed`, cause `resume`), or, with no prompt configured or a fresh launch, fails with
`service_shutdown`. Every other `stop` fails the turn with `session_stopped`.

Turns (D13): one at a time. `submit` reserves the slot and persists the record before the first
byte is written, then pastes the prompt (with a `<!-- ccw turn: <id> -->` marker, because Stop
payloads carry no turn id) and sends CR as a separate write. Text claude's parser would run as a
slash command (`slash_command`, design D2) is refused first, for every origin: claude runs no Stop
for a command, so such a turn would never end. A turn ends exactly once, on the
first of: a matching Stop (`complete`), an interrupt (resolved by the transcript interrupt line or
a second ESC after `interrupt_settle_seconds`, since claude runs no Stop hook for an interrupted
turn), a fatal pattern on screen, claude exiting (unless the turn is held for continuation, then
when the relaunch is dead, fails to boot or is fresh), the hard `turn_max_seconds` limit (measured
from the original submit, across a crash), or `stop`. Every end frees the slot, persists the
record and calls `on_turn_end`. A quiet turn is only reported (`on_stall`), never failed:
silence is not a stall.

The screen is never the source of turn results; it is read only for boot, fatal patterns and
debug tails, and every tail that leaves this module is redacted. A slash command run also reads
it for dialog footers (`watch_dialogs`): `dialog_on_screen` is a footer seen since the last ESC,
and `send_escape` never sends two ESCs within `ESC_SPACING_SECONDS` (a double ESC at an empty
prompt opens claude's rewind menu).
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import fnmatch
import inspect
import logging
import os
import re
import time
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from .claude_env import SECRET_KEY_RE, ClaudeEnv
from .config import COMMAND_NAME, BootConfig, BootDialog, LimitsConfig, SessionsConfig
from .pty_proc import PtyClosed, PtyProcess
from .redact import Redactor
from .ring import Ring, _squash, strip_ansi
from .turns import (
    PROBE_ANSWER_MAX,
    Origin,
    Probe,
    TurnRecord,
    TurnState,
    merged_annotations,
    new_message_id,
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
#: Why a session stops: a user stop or delete (`stop`), or the service shutting down.
StopCause = Literal["stop", "shutdown"]
#: The error of a turn a shutdown cut off that the resumed session cannot continue.
SERVICE_SHUTDOWN = "service_shutdown"
#: Minimum gap between two ESCs: a double ESC at an empty prompt opens the rewind menu.
ESC_SPACING_SECONDS = 1.0


def default_probe_text(token: str) -> str:
    return f"Reply with {token} and one sentence on what you are doing, then continue."


DEFAULT_PROBE_TEXT = default_probe_text("CCW-ALIVE")
#: The turn watchdog re-checks at least this often, so a cleared stall re-arms promptly.
TURN_WATCH_MAX_SLEEP = 0.25
#: `note_activity` appends `last_activity_at` to turns.jsonl at most this often (it runs on every
#: event); the store serves every update at once.
ACTIVITY_PERSIST_SECONDS = 5.0
#: Probes remembered for `get_probe`; the oldest are forgotten first.
MAX_PROBES = 100
#: Queued messages remembered for `queued_message`; the oldest are forgotten first.
MAX_QUEUED = 100

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


# Claude parses slash text with JavaScript `trim` and `search(/\s/)`, so the whitespace set is
# exactly JS `\s`, spelled out here: Python's `str.isspace` and regex `\s` differ (they add
# U+001C-001F and U+0085 and lack U+FEFF), so neither is used by the parser.
_JS_WHITESPACE = frozenset(
    chr(code)
    for code in (
        *range(0x09, 0x0E),
        0x20,
        0xA0,
        0x1680,
        *range(0x2000, 0x200B),
        0x2028,
        0x2029,
        0x202F,
        0x205F,
        0x3000,
        0xFEFF,
    )
)
_JS_WHITESPACE_CHARS = "".join(sorted(_JS_WHITESPACE))
_JS_WHITESPACE_CLASS = "".join(f"\\u{ord(char):04x}" for char in sorted(_JS_WHITESPACE))

# A trailing agents-common run marker (`agents_common.runs.marker`, `<!-- agents-<kind>:<id> -->`)
# as `runs.py:_post_once` posts it: `text\n\nmarker`.
_RUN_MARKER = re.compile(
    rf"\n\n<!-- agents-[^{_JS_WHITESPACE_CLASS}:]+:[^{_JS_WHITESPACE_CLASS}]+ -->"
    rf"[{_JS_WHITESPACE_CLASS}]*\Z"
)


def _ignorable(char: str) -> bool:
    """A control or format character (Unicode Cc or Cf) that is not JS whitespace."""
    return char not in _JS_WHITESPACE and unicodedata.category(char) in ("Cc", "Cf")


def slash_command(text: str) -> tuple[str, str] | None:
    """`(name, args)` when claude's own parser would run `text` as a slash command, else None.

    Design D2, mirroring claude 2.1.304 (`trim`, then a leading "/", the name up to the first
    `\\s`, the args trimmed): a trailing run marker is dropped; leading JS whitespace and
    invisible control or format characters (Cc, Cf: ESC, zero-width, bidi, NUL, DEL, C1) are
    skipped, because the PTY paste drops ESC and claude ignores the rest; the text must then start
    with "/". The name runs to the first JS whitespace character with the invisible characters
    dropped from it, and the args are the rest stripped of JS whitespace only (newlines kept). An
    empty name is a command (claude answers "Commands are in the form /command [args]"), and so
    is a name matching `COMMAND_NAME`, with no length cap. Any other name (a path such as
    "/etc/hosts") is prose that claude sends to the model.
    """
    text = _RUN_MARKER.sub("", text)
    start = 0
    while start < len(text) and (text[start] in _JS_WHITESPACE or _ignorable(text[start])):
        start += 1
    if not text.startswith("/", start):
        return None
    end = start + 1
    while end < len(text) and text[end] not in _JS_WHITESPACE:
        end += 1
    name = "".join(char for char in text[start + 1 : end] if not _ignorable(char))
    if name and not COMMAND_NAME.fullmatch(name):
        return None
    return name, text[end:].strip(_JS_WHITESPACE_CHARS)


class TurnRejected(Exception):
    """A turn or probe cannot be written now. `reason`: busy, booting, dead, or slash_command
    (the text is a slash command, which goes to the command queue, never typed as a turn)."""

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


MANAGED_ENV_NAMES = frozenset(
    {
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_PROJECT_DIR_NAME",
        "GIT_CONFIG_GLOBAL",
        "CCW_SESSION_ID",
        "CCW_INTERNAL_URL",
        "CCW_HOOK_TOKEN",
        "CCW_TOOL_GATE_FAIL",
        "CCW_GATE_TIMEOUT",
    }
)


def passthrough_names(
    service_env: Mapping[str, str], patterns: Iterable[str], exclude: Iterable[str] = ()
) -> list[str]:
    """Service env names selected by `fnmatchcase` patterns minus the exclude patterns.

    `CCW_*` names and service-managed names are never selected, even through `*`; a
    refused literal pattern is logged. Shared by `build_env` and the setup-command env.
    """
    excludes = list(exclude)
    chosen: list[str] = []
    for pattern in patterns:
        if pattern.startswith("CCW_") or pattern in MANAGED_ENV_NAMES:
            log.warning("passthrough of %s refused: service-managed or service-private", pattern)
        for name in service_env:
            if name in chosen or name.startswith("CCW_") or name in MANAGED_ENV_NAMES:
                continue
            if fnmatch.fnmatchcase(name, pattern) and not any(
                fnmatch.fnmatchcase(name, e) for e in excludes
            ):
                chosen.append(name)
    return sorted(chosen)


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
    passthrough_exclude: Iterable[str] = (),
    peers: bool = False,
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
    if peers:
        managed["CLAUDE_CODE_HARBOR_KITE"] = "0"
    env = {name: service_env[name] for name in BASE_ENV if name in service_env}
    for name in passthrough_names(service_env, passthrough, passthrough_exclude):
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

    def on_turn_resumed(self, record: Any, restart: int, cause: str = "restart") -> None:
        """A held turn was continued: `cause` `restart` after the crash relaunch number
        `restart`, `resume` in a session resumed after a shutdown cut the turn off."""

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


def _age_seconds(record: TurnRecord) -> float:
    """Wall-clock seconds since the turn was submitted (0 for an unreadable `started_at`)."""
    try:
        started = datetime.fromisoformat(record.started_at).timestamp()
    except ValueError:
        return 0.0
    return max(time.time() - started, 0.0)


@dataclass
class QueuedMessage:
    """A message typed into a running turn (`queue_prompt`): claude queues it and hands it to
    the model at its next tool boundary, or runs it as its own turn when the turn ends first."""

    message_id: str
    lane: str
    text: str
    origin: Origin
    #: The turn that was running when the message was typed.
    turn_id: str
    queued_at: str
    callback_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    #: The turn the model read it in, once its marker reached the transcript.
    delivered_turn_id: str | None = None


@dataclass
class _PendingInterrupt:
    """An interrupt in flight for `record`; `final` is the state the turn ends in."""

    record: TurnRecord
    final: TurnState
    #: Every ESC keeps `ESC_SPACING_SECONDS` from the previous one (a priority delivery).
    spaced: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Future[None] | None = None


class Session:
    """One claude process (relaunched as needed), its boot state machine and its turn slot.

    `state` is internal (`STATES`); `snapshot()["state"]` and `on_state` use `PUBLIC_STATES`.
    `pty` is the current `PtyProcess`, `ring` the screen shared across relaunches, and
    `transcript_path` the conversation file (the router may replace it with the path a hook
    payload reports). `turn_store` (a `TurnStore`) receives every change of every turn record.
    `resume_turn` is a `running` record of an earlier process (a shutdown or a kill cut it off):
    the session holds it from the start and `start` continues or fails it.
    """

    def __init__(
        self,
        spec: SessionSpec,
        listener: SessionListener,
        *,
        turn_store: Any = None,
        clock: Callable[[], float] = time.monotonic,
        resume_turn: TurnRecord | None = None,
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
        self.state_since: str | None = None  # when the public state last changed
        self.idle_since: str | None = None  # state_since while public state is ready, else None
        self.boot_error: BootError | None = None
        self._redactor = _redactor_for(spec.env)
        self._scanner = FatalScanner(spec.boot.fatal_patterns)
        #: Dialog footers of the slash command being run (`watch_dialogs`); none otherwise.
        self._dialogs = FatalScanner(())
        self._dialog_hits = 0
        self._hits_at_escape = 0
        self._last_escape = float("-inf")
        #: Dialog footers on screen at any time (permission prompts, questions), for the lanes:
        #: when the last one was seen, the last relayed hook, and the last text typed (squashed;
        #: its echo is not a dialog).
        self._ui_dialogs = FatalScanner(spec.sessions.commands.dialog_patterns)
        self._ui_dialog_at = float("-inf")
        self._last_hook = float("-inf")
        self._typed = ""
        #: Set while `restart_for_dialog` closes claude, so its exit takes the dialog relaunch.
        self._dialog_restart = False
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._exited = asyncio.Event()
        self._public: str | None = None
        self._stopping = False
        self._recovery: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Future[Any]] = set()
        self._turn: TurnRecord | None = None
        self._turn_watch: asyncio.Future[None] | None = None
        self._interrupting: _PendingInterrupt | None = None
        #: The running turn while claude is relaunched after a crash (continuation, K12).
        self._held: TurnRecord | None = None
        #: The running turn while the router runs its slash commands (`hold_for_commands`).
        self._commands_turn: TurnRecord | None = None
        self._last_activity = clock()
        #: Session clock time of the last PTY output (the runner's quiet-screen wait).
        self.last_output = clock()
        #: Session clock time of the last command line, continuation or key written.
        self.last_input = clock()
        #: A slash command run holds the slot with no turn record (`begin_command`).
        self._command_active = False
        self._turn_started = clock()
        self._activity_persisted = self._last_activity
        self._probes: dict[str, Probe] = {}
        self._queued: dict[str, QueuedMessage] = {}
        #: The slot is held for a priority delivery (`reserve`).
        self._reserved = False
        if resume_turn is not None:
            # The slot is taken from the first boot; `start` continues or fails the turn.
            resume_turn.interrupted = None
            self._turn = self._held = resume_turn
            self._turn_started = clock() - _age_seconds(resume_turn)
        listener.bind(self)

    # -- state and reporting ---------------------------------------------------------------

    def _set_state(self, state: str) -> None:
        public = public_state(state)
        self.state = state
        if state not in ("ready", "busy"):
            self._command_active = False  # a command run cannot outlive the live claude
        if public != self._public:
            self._public = public
            self.state_since = now_iso()
            self.idle_since = self.state_since if public == "ready" else None
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
            "idle_since": self.idle_since,
            "state_since": self.state_since,
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
        """Launch claude and return once it is idle at its prompt; raises `BootError`.

        A `resume_turn` is continued once claude is ready (cause `resume`) when the launch
        resumed and `restart_continue_prompt` is set, else it fails with `service_shutdown`, as
        it does when claude never boots. `turn_max_seconds` counts from its original submit.
        """
        if self.started_at is not None:
            raise RuntimeError("Session.start() may be called only once")
        self.started_at = now_iso()
        self._set_state("booting")
        adopted = self._held
        if adopted is not None:
            self._persist(adopted)  # held by this session now: the shutdown marker is cleared
            self._arm_watch(adopted, _age_seconds(adopted))
        try:
            resumed = await self._boot(
                resume=self.spec.resume or os.path.isfile(self.transcript_path)
            )
        except BootError as exc:
            if exc.code != "stopped":  # a stop decided the held turn itself
                self._fail_held(SERVICE_SHUTDOWN)
            raise
        if adopted is None or self._held is not adopted or self._stopping:
            return
        if resumed and self.spec.sessions.restart_continue_prompt:
            await self._continue_held(adopted, cause="resume")
        else:  # nothing to paste, or a fresh claude has no conversation to continue
            self._end_turn(adopted, "failed", error=SERVICE_SHUTDOWN)

    async def _boot(self, *, resume: bool, slot: bool | None = None) -> bool:
        """Launch and wait for ready, relaunching once; returns whether the launch resumed.

        The first launch resumes when `resume`; the relaunch only when the transcript
        exists. Two failed boots in a row leave the session dead and raise `BootError`.
        `slot` is set for a dialog relaunch: whether a command run held the slot. Then claude
        that is ready goes back to busy for the command run or the running turn, never ready.
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
                if slot is not None and (slot or self._turn is not None):
                    self._command_active = slot
                    self._set_state("busy")
                else:
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
        self._dialogs.reset()
        self._ui_dialogs.reset()
        self._ui_dialog_at = float("-inf")
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
        self.last_output = self.clock()
        self._notify("on_output", text)
        self._dialog_hits += len(self._dialogs.feed(text))
        for pattern in self._ui_dialogs.feed(text):
            if _squash(pattern) not in self._typed:
                self._ui_dialog_at = self.last_output
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
        if self._dialog_restart:
            self._dialog_restart = False
            log.warning("session %s: relaunching claude over an open dialog", self.spec.session_id)
            slot = self._command_active  # read before booting clears it
            self.ring.reset()
            self._set_state("booting")
            self._recovery = asyncio.ensure_future(self._recover(pty, code, slot=slot))
            self._track(self._recovery)
            return
        self._commands_turn = None  # a held turn continues with the restart prompt
        record = self._turn
        if record is not None:
            if (
                self.restarts < self.spec.max_restarts
                and self.spec.sessions.restart_continue_prompt
            ):
                self._held = record  # stays running; _recover continues or fails it
            else:  # never re-pasted: the client decides whether to retry
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

    async def _recover(self, old: PtyProcess, code: int, *, slot: bool | None = None) -> None:
        """claude exited on its own after boot: relaunch within the budget, else dead.

        `slot` is set for a dialog relaunch (`restart_for_dialog`): no budget is spent, and the
        turn whose commands run fails `claude_exited` when claude does not come back resumed.
        """
        await old.close(DEAD_CLOSE_GRACE)
        if self._stopping:
            return
        if slot is not None:
            self._held = self._turn  # failed below unless the relaunch resumes
        elif self.restarts >= self.spec.max_restarts:
            log.error(
                "session %s: restart budget (%d) exhausted",
                self.spec.session_id,
                self.spec.max_restarts,
            )
            self._fail_held()
            self._set_state("dead")
            self._notify("on_exit", code)
            return
        else:
            self.restarts += 1
        self.ring.reset()
        self._set_state("booting")
        try:
            resumed = await self._boot(resume=os.path.isfile(self.transcript_path), slot=slot)
        except BootError as exc:
            if exc.code != "stopped":  # a stop ended the held turn itself
                self._fail_held()
                self._notify("on_exit", code)
            return
        if not resumed:
            self._fail_held()  # a fresh claude has no conversation to continue
        if slot is not None:
            self._held = None  # the router goes on with the turn's commands
        self._notify("on_restart", self.restarts, resumed)
        if resumed and self._held is not None and not self._stopping:
            await self._continue_held(self._held)

    def _fail_held(self, error: str = "claude_exited") -> None:
        record = self._held
        if record is not None:
            self._end_turn(record, "failed", error=error, next_state=None)

    async def _continue_held(self, record: TurnRecord, cause: str = "restart") -> None:
        """Retake the slot for the held turn and paste the continue prompt with its marker."""
        self._held = None
        self._set_state("busy")
        self._notify("on_turn_resumed", record, self.restarts, cause)
        log.info(
            "session %s: turn %s continued (%s, restart %d)",
            self.spec.session_id,
            record.turn_id,
            cause,
            self.restarts,
        )
        await self.continue_turn(record, self.spec.sessions.restart_continue_prompt)

    async def continue_turn(self, record: TurnRecord, text: str) -> bool:
        """Resume the running turn `record`: paste `text` with its turn marker, then CR.

        Claude's next Stop carries the same marker, so it completes the same record; nothing
        about the record is reset. Ends the command phase (`hold_for_commands`). False, and
        nothing is written, unless `record` is the running turn of a live claude; a write that
        fails ends the turn `failed` with `claude_exited`.
        """
        pty = self.pty
        if (
            pty is None
            or record.terminal
            or record is not self._turn
            or self.state not in ("ready", "busy")
        ):
            return False
        if self._commands_turn is record:
            self._commands_turn = None
        self._set_state("busy")
        try:
            self._typed = _squash(text)
            await pty.send_prompt(
                self._with_marker(text, record), self.spec.boot.submit_delay_seconds
            )
        except PtyClosed:
            self._end_turn(
                record, "failed", error="claude_exited", next_state="ready" if pty.alive else None
            )
            return False
        finally:
            self.last_input = self.clock()
        return True

    # -- stop ------------------------------------------------------------------------------

    async def stop(self, grace: float = 5.0, *, cause: StopCause = "stop") -> None:
        """Terminate claude (SIGTERM, then SIGKILL after `grace`); nothing relaunches after.

        The running turn fails with `session_stopped`; on a service shutdown it stays running
        for a later resume instead (`_interrupt_for_shutdown`).
        """
        if self.state == "stopped":
            return
        self._stopping = True
        record = self._turn
        if record is not None:
            if cause == "shutdown":
                self._interrupt_for_shutdown(record)
            else:
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

    def _interrupt_for_shutdown(self, record: TurnRecord) -> None:
        """Release the running turn without ending it: `running`, `interrupted="shutdown"`.

        No `on_turn_end`, so no `turn_failed`, hook or callback; the resumed session continues
        it. A turn already being interrupted (a client interrupt, `turn_max_seconds`) ends as
        that interrupt would have ended it, and is never continued.
        """
        pending = self._interrupting
        if pending is not None and pending.record is record:
            self._end_turn(record, pending.final, next_state=None)
            return
        self._turn = self._held = self._commands_turn = None
        watch, self._turn_watch = self._turn_watch, None
        if watch is not None and watch is not asyncio.current_task():
            watch.cancel()
        record.interrupted = "shutdown"
        log.info(
            "session %s: turn %s left running for a resume (service shutdown)",
            self.spec.session_id,
            record.turn_id,
        )
        self._persist(record)

    # -- turns -----------------------------------------------------------------------------

    @property
    def current_turn(self) -> TurnRecord | None:
        """The running turn's live record, if any."""
        return self._turn

    @property
    def turn_elapsed(self) -> float | None:
        """Seconds the running turn has run on the session clock, None when idle."""
        if self._turn is None:
            return None
        return max(self.clock() - self._turn_started, 0.0)

    def _writable_pty(self) -> PtyProcess:
        """The PTY a turn or probe may be written to, else `TurnRejected(booting|dead)`."""
        if self.state == "booting":
            raise TurnRejected("booting")
        if self.state not in ("ready", "busy") or self.pty is None:
            raise TurnRejected("dead")
        return self.pty

    def check_submittable(self, *, reserved: bool = False) -> PtyProcess:
        """Raise `TurnRejected(booting|dead|busy)` if a turn cannot start now (no side effects).

        While the slot is reserved (`reserve`) only the holder (`reserved=True`) may start one.
        """
        pty = self._writable_pty()
        if self.state == "busy" or self._turn is not None or (self._reserved and not reserved):
            raise TurnRejected("busy")
        return pty

    @property
    def reserved(self) -> bool:
        return self._reserved

    @contextlib.contextmanager
    def reserve(self) -> Iterator[None]:
        """Hold the turn slot across an interrupt and the next submit (a priority delivery).

        Meanwhile other turns (`check_submittable`) and slash command runs (`begin_command`)
        are refused busy, so nothing takes the slot the interrupt freed. A turn claude starts by
        itself (`deliver_message`) is still accounted.
        """
        self._reserved = True
        try:
            yield
        finally:
            self._reserved = False

    async def submit(
        self,
        text: str,
        *,
        origin: Origin = "client",
        callback_url: str | None = None,
        turn_id: str | None = None,
        annotations: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        reserved: bool = False,
    ) -> TurnRecord:
        """Start a turn: reserve the slot, persist the record, then paste and CR.

        `turn_id` is an id allocated earlier (the router names it in the onTurn payload).
        Raises `TurnRejected` (slash_command, for any origin and before anything else, then
        booting, dead, busy). A claude that exits under the write fails the turn with
        `claude_exited`; the record is returned either way.
        """
        if slash_command(text) is not None:
            # claude would run it as a command: no Stop, so the turn would never end (design D3).
            raise TurnRejected("slash_command")
        pty = self.check_submittable(reserved=reserved)
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
            metadata=dict(metadata or {}),
        )
        # Reserve before the first byte: a second submit racing this one sees busy.
        self._turn = record
        self._turn_started = self.clock()
        self._last_activity = self._activity_persisted = self.clock()
        self._set_state("busy")
        self._persist(record)
        self._arm_watch(record)
        log.info("session %s: turn %s submitted (%s)", self.spec.session_id, record.turn_id, origin)
        try:
            self._typed = _squash(text)
            await pty.send_prompt(
                self._with_marker(text, record), self.spec.boot.submit_delay_seconds
            )
        except PtyClosed:
            # A claude that died is handled by the exit path (relaunch or dead); a live one
            # whose write failed gets its slot back here.
            self._end_turn(
                record, "failed", error="claude_exited", next_state="ready" if pty.alive else None
            )
        return record

    def check_live(self) -> None:
        """Raise `TurnRejected(booting|dead)` unless claude is live (no side effects)."""
        self._writable_pty()

    def note_hook(self) -> None:
        """A claude hook was relayed: claude is past any dialog it showed before it."""
        self._last_hook = self.clock()

    def dialog_open(self) -> bool:
        """A dialog footer (`sessions.commands.dialog_patterns`: a permission prompt, a
        question) appeared on screen after the last relayed hook and the last ESC.

        Typing into it would pick an answer, and an ESC would decline it, so the lanes refuse
        to write while it is open. Footers that the last typed text quotes are its echo.
        """
        return self.state in ("ready", "busy") and self._ui_dialog_at > max(
            self._last_hook, self._last_escape
        )

    def check_no_dialog(self) -> None:
        """Raise `TurnRejected("dialog_open")` while `dialog_open` (no side effects)."""
        if self.dialog_open():
            raise TurnRejected("dialog_open")

    def check_queueable(self) -> PtyProcess:
        """The PTY a message may be typed into: a live claude running a turn of its own.

        Raises `TurnRejected` booting or dead, then dialog_open, else busy when no turn runs
        or a slash command run, its command phase or a crash relaunch holds the slot (typing
        then would reach a command line or a dialog, not the model). No side effects.
        """
        pty = self._writable_pty()
        self.check_no_dialog()
        record = self._turn
        if (
            self.state != "busy"
            or record is None
            or self._command_active
            or self._commands_turn is not None
            or self._held is record
        ):
            raise TurnRejected("busy")
        return pty

    async def queue_prompt(
        self,
        text: str,
        *,
        lane: str,
        origin: Origin,
        message_id: str | None = None,
        callback_url: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> QueuedMessage:
        """Type `text` into the running turn: paste with a message marker, then one CR.

        No ESC and no turn marker: claude queues the text (Enter while busy) and passes it to
        the model within the same turn, so the turn's Stop still completes the running record.
        Raises `TurnRejected` (slash_command first, then `check_queueable`); a write to a
        claude that exited raises `TurnRejected("dead")`.
        """
        if slash_command(text) is not None:
            raise TurnRejected("slash_command")
        pty = self.check_queueable()
        record = self._turn
        assert record is not None
        message = QueuedMessage(
            message_id=message_id or new_message_id(),
            lane=lane,
            text=text,
            origin=origin,
            turn_id=record.turn_id,
            queued_at=now_iso(),
            callback_url=callback_url,
            metadata=dict(metadata or {}),
        )
        self._queued[message.message_id] = message
        while len(self._queued) > MAX_QUEUED:
            del self._queued[next(iter(self._queued))]
        prompt = f"{text}\n\n<!-- ccw message: {message.message_id} -->"
        try:
            self._typed = _squash(prompt)
            await pty.send_prompt(prompt, self.spec.boot.submit_delay_seconds)
        except PtyClosed as exc:
            self._queued.pop(message.message_id, None)
            raise TurnRejected("dead") from exc
        finally:
            self.last_input = self.clock()
        log.info(
            "session %s: message %s queued into %s (%s)",
            self.spec.session_id,
            message.message_id,
            record.turn_id,
            lane,
        )
        return message

    def queued_message(self, message_id: str) -> QueuedMessage | None:
        return self._queued.get(message_id)

    def undelivered(self, turn_id: str) -> list[QueuedMessage]:
        """Messages typed into the turn `turn_id` whose marker has not been read yet."""
        return [
            m for m in self._queued.values() if m.turn_id == turn_id and m.delivered_turn_id is None
        ]

    def deliver_message(self, message_id: str) -> tuple[QueuedMessage, bool] | None:
        """The message's marker reached the transcript: `(message, new_turn)`, else None.

        Read while its turn (or any turn) runs, the model took it within that turn. Read on a
        ready session, claude ran it as a turn of its own after the turn it was typed into
        ended: that becomes a new running turn here (`new_turn` True; origin, text, callback
        and metadata from the message), so the next Stop completes it. None for an unknown or
        already delivered id, or when claude is not live or a command run holds the slot.
        """
        message = self._queued.get(message_id)
        if message is None or message.delivered_turn_id is not None:
            return None
        record = self._turn
        if self.state == "busy" and record is not None:
            message.delivered_turn_id = record.turn_id
            return message, False
        if self.state != "ready" or record is not None or self._command_active:
            return None
        now = now_iso()
        record = TurnRecord(
            turn_id=new_turn_id(),
            session_id=self.spec.session_id,
            origin=message.origin,
            state="running",
            text=message.text,
            callback_url=message.callback_url,
            started_at=now,
            last_activity_at=now,
            metadata=dict(message.metadata),
        )
        self._turn = record
        self._turn_started = self.clock()
        self._last_activity = self._activity_persisted = self.clock()
        self._set_state("busy")
        self._persist(record)
        self._arm_watch(record)
        message.delivered_turn_id = record.turn_id
        log.info(
            "session %s: message %s runs as turn %s",
            self.spec.session_id,
            message.message_id,
            record.turn_id,
        )
        return message, True

    def _with_marker(self, text: str, record: TurnRecord) -> str:
        if not self.spec.sessions.turn_marker:
            return text
        return f"{text}\n\n<!-- ccw turn: {record.turn_id} -->"

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

    def _arm_watch(self, record: TurnRecord, elapsed: float = 0.0) -> None:
        if self.spec.sessions.turn_stall_seconds > 0 or self.spec.limits.turn_max_seconds > 0:
            self._turn_watch = self._track(asyncio.ensure_future(self._watch_turn(record, elapsed)))

    async def _watch_turn(self, record: TurnRecord, elapsed: float = 0.0) -> None:
        """Report a quiet turn once per quiet period; end it as `timeout` at the hard limit.

        It keeps running while the turn is held across a crash, so both clocks measure from
        the original submit; a held turn at the limit just ends (there is no claude to ESC).
        `elapsed` is how long the turn already ran before this session held it (a resume).
        """
        stall = self.spec.sessions.turn_stall_seconds
        limit = self.spec.limits.turn_max_seconds
        started = self.clock() - elapsed
        while self._turn is record:
            now = self.clock()
            if limit > 0 and now - started >= limit:
                log.warning(
                    "session %s: turn %s hit turn_max_seconds=%g, interrupting",
                    self.spec.session_id,
                    record.turn_id,
                    limit,
                )
                if self._held is record:
                    self._end_turn(record, "timeout", next_state=None)
                else:
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
        if self._held is record:
            self._held = None  # nothing is pasted for it after the relaunch
        if self._commands_turn is record:
            self._commands_turn = None
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

    # -- slash commands --------------------------------------------------------------------

    @property
    def command_active(self) -> bool:
        """A slash command run holds the slot (busy with no turn record)."""
        return self._command_active

    def begin_command(self) -> bool:
        """Take the slot for a slash command run on an idle session: ready -> busy, no turn.

        Client turns then get `TurnRejected("busy")` and the idle reaper (ready only) skips
        the session. False, and nothing changes, unless the session is ready with no turn.
        """
        if self.state != "ready" or self._turn is not None or self._command_active:
            return False
        if self._reserved:
            return False
        self._command_active = True
        self._set_state("busy")
        return True

    def adopt_turn(self, text: str, *, origin: Origin) -> TurnRecord:
        """The slash command line `text` the runner typed made claude run a model turn (D8).

        A running record is created and persisted, and the turn watch armed, while the slot
        `begin_command` took stays held: the session stays busy and no `session_state` is
        emitted. The slot passes from the command run to the turn, so `end_command` leaves busy
        to it, and the turn ends like any turn (Stop, interrupt, `turn_max_seconds`). Raises
        `TurnRejected` (booting, dead, or busy when no command run holds the slot).
        """
        self._writable_pty()
        if self._turn is not None or not self._command_active or self.state != "busy":
            raise TurnRejected("busy")
        now = now_iso()
        record = TurnRecord(
            turn_id=new_turn_id(),
            session_id=self.spec.session_id,
            origin=origin,
            state="running",
            text=text,
            started_at=now,
            last_activity_at=now,
        )
        self._command_active = False
        self._turn = record
        self._turn_started = self.clock()
        self._last_activity = self._activity_persisted = self.clock()
        self._persist(record)
        self._arm_watch(record)
        log.info(
            "session %s: command turn %s adopted (%s)", self.spec.session_id, record.turn_id, origin
        )
        return record

    def end_command(self) -> None:
        """Release the slot `begin_command` took; busy -> ready when no turn took over."""
        if not self._command_active:
            return
        self._command_active = False
        if self.state == "busy" and self._turn is None:
            self._set_state("ready")

    async def wait_quiet(self, seconds: float, timeout: float) -> bool:
        """Wait until the PTY has been silent for `seconds`; False after `timeout`.

        Silent every way: no output, nothing written (a command line, a continuation, an ESC)
        and no hook relay or transcript line (`note_activity`) for `seconds`, so claude has read
        the last write and finished the hook it was in before the next line is typed.
        """
        deadline = self.clock() + timeout
        while True:
            now = self.clock()
            quiet = now - max(self.last_output, self.last_input, self._last_activity)
            if quiet >= seconds:
                return True
            if now >= deadline:
                return False
            await asyncio.sleep(max(min(seconds - quiet, deadline - now), 0.01))

    async def send_command(self, line: str) -> None:
        """Type a slash command line: paste then CR, no turn marker, under the PTY write lock.

        Raises `TurnRejected(booting|dead)` or `PtyClosed`.
        """
        pty = self._writable_pty()
        log.info("session %s: typing %r", self.spec.session_id, line)
        try:
            self._typed = _squash(line)
            await pty.send_prompt(line, self.spec.boot.submit_delay_seconds)
        finally:
            self.last_input = self.clock()

    async def send_escape(self) -> None:
        """One ESC for a slash command (a dialog, a timeout, an interrupt), never within
        `ESC_SPACING_SECONDS` of the previous ESC; nothing while claude is not live."""
        wait = self._last_escape + ESC_SPACING_SECONDS - self.clock()
        if wait > 0:
            await asyncio.sleep(wait)
        if self.state in ("ready", "busy"):
            await self._send_key(ESC)

    def watch_dialogs(self, patterns: Iterable[str], typed: str = "") -> None:
        """Scan the PTY output from now on for these dialog footers (none: stop scanning).

        A pattern the typed line itself holds is left out: the line is echoed on screen.
        """
        line = _squash(typed)
        self._dialogs = FatalScanner(p for p in patterns if not line or _squash(p) not in line)
        self._dialog_hits = self._hits_at_escape = 0

    @property
    def last_escape(self) -> float:
        """`clock()` time of the last ESC written (-inf before any)."""
        return self._last_escape

    @property
    def dialog_hits(self) -> int:
        """Dialog footers seen since `watch_dialogs`."""
        return self._dialog_hits

    def dialog_on_screen(self) -> bool:
        """A dialog footer was seen since the last ESC (or since `watch_dialogs`)."""
        return self._dialog_hits > self._hits_at_escape

    async def restart_for_dialog(self) -> bool:
        """Relaunch claude because a dialog stayed open; True once claude is back.

        The crash path (`_on_exit`, `_recover`) relaunches it, with `--resume` of the same
        claude session id when the transcript exists, and `on_restart` is notified. `restarts`
        is unchanged, and the slot stays taken: the session is `booting` meanwhile, then `busy`
        again for the command run or the turn whose commands run. A relaunch that fails leaves
        the session dead, as a crash would.
        """
        pty = self.pty
        if pty is None or self._stopping or self.state not in ("ready", "busy"):
            return False
        self._dialog_restart = True
        try:
            await pty.close(DEAD_CLOSE_GRACE)
        finally:
            self._dialog_restart = False
        recovery = self._recovery
        if recovery is not None and not recovery.done():
            await asyncio.shield(recovery)
        return self.state in ("ready", "busy")

    @property
    def commands_turn(self) -> TurnRecord | None:
        """The running turn whose Stop was answered while slash commands run for it."""
        return self._commands_turn

    def hold_for_commands(self, record: TurnRecord) -> bool:
        """Mark the command phase of the running turn `record` (its Stop was answered allow).

        The turn stays running and the session busy; claude is at its prompt with no turn of
        its own until `continue_turn` (or `release_commands_hold`, when a prompt command made
        claude run a model turn). Meanwhile an interrupt or `turn_max_seconds` ends the
        turn at once (no ESC from here: the router ESCs the command it is running), and a
        transcript interrupt line is the command's. False unless `record` is the running turn.
        """
        if record.terminal or record is not self._turn or self.state != "busy":
            return False
        self._commands_turn = record
        return True

    def release_commands_hold(self, record: TurnRecord) -> None:
        """End the command phase of `record` with no continue prompt (design D8).

        A prompt command typed in that phase made claude run a model turn, and `record`
        continues as that turn: from here an interrupt sends ESC like any running turn and a
        transcript interrupt line is the turn's. No-op unless `record` is held for commands.
        """
        if self._commands_turn is record:
            self._commands_turn = None

    # -- interrupt -------------------------------------------------------------------------

    async def interrupt(self, *, spaced: bool = False) -> str | None:
        """Interrupt the running turn; its id once it has ended, None when idle.

        One ESC, then up to `interrupt_settle_seconds` for the transcript interrupt line
        (`on_interrupted`); without it a second ESC. claude runs no Stop hook for an
        interrupted turn, so the turn is ended here either way and the slot freed. A turn held
        across a crash ends at once: there is no claude to send ESC to, and nothing is pasted.
        `spaced` (a priority delivery) keeps every ESC `ESC_SPACING_SECONDS` from the last one.
        """
        record = self._turn
        if record is not None and self._held is record:
            self._end_turn(record, "interrupted", next_state=None)
            return record.turn_id
        if record is None or self.state != "busy":
            return None
        return await self._interrupt(record, "interrupted", spaced=spaced)

    async def _interrupt(
        self, record: TurnRecord, final: TurnState, *, spaced: bool = False
    ) -> str:
        pending = self._interrupting
        if pending is None or pending.record is not record:
            pending = _PendingInterrupt(record, final, spaced)
            self._interrupting = pending
            pending.task = self._track(asyncio.ensure_future(self._run_interrupt(pending)))
        assert pending.task is not None
        # Shielded: a caller that goes away (a dropped request) cannot strand the slot.
        await asyncio.shield(pending.task)
        return record.turn_id

    async def _run_interrupt(self, pending: _PendingInterrupt) -> None:
        record = pending.record
        try:
            if self._commands_turn is record:
                # claude runs no turn: there is no interrupt line to wait for.
                self._end_turn(record, pending.final)
                return
            await self._interrupt_key(pending)
            try:
                await asyncio.wait_for(
                    pending.done.wait(), self.spec.sessions.interrupt_settle_seconds
                )
            except TimeoutError:
                # A turn held across a crash: the second ESC would hit the booting relaunch.
                if not record.terminal and self._held is not record:
                    log.warning(
                        "session %s: no interrupt line for %s after %gs, second ESC",
                        self.spec.session_id,
                        record.turn_id,
                        self.spec.sessions.interrupt_settle_seconds,
                    )
                    await self._interrupt_key(pending)
            self._end_turn(record, pending.final)
        finally:
            if self._interrupting is pending:
                self._interrupting = None

    async def _interrupt_key(self, pending: _PendingInterrupt) -> None:
        if pending.spaced:
            await self.send_escape()
        else:
            await self._send_key(ESC)

    async def _send_key(self, key: bytes) -> None:
        pty = self.pty
        if pty is None:
            return
        with contextlib.suppress(PtyClosed):  # the exit path ends the turn
            await pty.write(key)
        self.last_input = self.clock()
        if key == ESC:
            self._last_escape = self.last_input
            self._hits_at_escape = self._dialog_hits

    def on_interrupted(self) -> None:
        """The transcript shows an interrupt: end the running turn (pending or unprompted)."""
        record = self._turn
        if record is None:
            return
        pending = self._interrupting
        if self._commands_turn is record and (pending is None or pending.record is not record):
            return  # an ESC'd slash command, not the turn
        final: TurnState = "interrupted"
        if pending is not None and pending.record is record:
            final = pending.final
        self._end_turn(record, final)

    # -- probes ----------------------------------------------------------------------------

    async def probe(self, text: str | None = None) -> Probe:
        """Write a liveness probe without taking the slot (ready or busy).

        The probe is pending until the router sees its marker in the transcript
        (`probe_delivered`) and answered once assistant text with the alive token follows
        (`probe_answered`, which records the answer text). Raises `TurnRejected` (slash_command
        first, for text claude would run as a command, then booting, dead); nothing is pasted.
        """
        if text is not None and slash_command(text) is not None:
            raise TurnRejected("slash_command")
        pty = self._writable_pty()
        probe = Probe(
            probe_id=new_probe_id(),
            text=text or default_probe_text(self.spec.sessions.probe_alive_token),
            created_at=now_iso(),
        )
        self._probes[probe.probe_id] = probe
        while len(self._probes) > MAX_PROBES:
            del self._probes[next(iter(self._probes))]
        prompt = f"{probe.text}\n\n<!-- ccw probe: {probe.probe_id} -->"
        try:
            self._typed = _squash(prompt)
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

    def probe_answered(self, answer: str = "") -> list[Probe]:
        """Main-chain assistant text with the alive token arrived: every delivered probe is
        answered, keeping the first `PROBE_ANSWER_MAX` characters of `answer`."""
        answered = [p for p in self._probes.values() if p.state == "delivered"]
        now = now_iso()
        for probe in answered:
            probe.state = "answered"
            probe.answered_at = now
            probe.answer = answer[:PROBE_ANSWER_MAX]
        return answered
