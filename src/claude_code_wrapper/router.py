"""Per-session event router: Claude hooks, transcript lines and session callbacks -> events.

One `SessionRouter` per session wires the `Session`, its `EventLog`, the user `HookDispatcher`,
the transcript tailers and the redactor (D6, D7, D11). It is the `SessionListener` of its
session, the target of the internal API's hook relay (`handle_claude_hook`) and the entry point
for turns (`submit_turn`, `interrupt`).

Callbacks (a turn's terminal state, a forwarded AskUserQuestion) POST the event envelope
`{seq, ts, session_id, type, data}` signed with `server.callback_secret`, so one receiver
handles both and tells them apart by `type`.

The router never breaks Claude: an exception in a hook handler becomes an `error` event and an
allow response. `session` is bound by `Session(spec, router)` (the `SessionListener.bind` call),
because the session and its listener are created together.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import time
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .command_signals import Signal, SignalBus
from .commands import CommandQueue, CommandRecord, CommandRejected, render, validate_params
from .commands import Origin as CommandOrigin
from .config import CommandsConfig, Config
from .events import Event as LogEvent
from .events import EventLog
from .hook_relay import RelayResponse, deny_output
from .hooks import ActionResult, HookDispatcher, HookOutcome, build_payload, gate_fail_mode
from .metrics import Metrics
from .pty_proc import PtyClosed
from .redact import Redactor
from .session import Session, SessionListener, TurnRejected
from .timeutil import now_iso
from .transcript import (
    MAX_RESULT_CHARS,
    TRUNCATED_SUFFIX,
    AssistantText,
    CommandInvoked,
    CommandOutput,
    CompactBoundary,
    Interrupted,
    ProbeMarker,
    SubagentWatcher,
    Thinking,
    TranscriptTailer,
    Usage,
    UsageTotals,
    UserPrompt,
)
from .transcript import Event as TranscriptEvent
from .turns import (
    TERMINAL_STATES,
    Origin,
    TurnRecord,
    TurnStore,
    merged_annotations,
    new_turn_id,
)
from .workdir import safe_write_files

if TYPE_CHECKING:
    from .peer_inbox import PeerInbox

log = logging.getLogger(__name__)

CCW_TOOL_PREFIX = "mcp__ccw__"
CCW_SUBAGENT_DENY_REASON = (
    "ccw tools are available to the main agent only; "
    "subagents and teammates cannot call mcp__ccw__* tools."
)
NATIVE_PEER_TOOLS = ("SendMessage", "ListAgents")
NATIVE_PEER_DENY_REASON = "use mcp__ccw__send_message / mcp__ccw__peers"
QUESTION_DENY_REASON = (
    "Question forwarded to the API client. Ask the user in plain text, "
    "end your turn now and wait for their reply in the next message."
)
DEFAULT_DENY_REASON = "Denied by a wrapper hook."
DEFAULT_BLOCK_REASON = "A wrapper onStop hook asked you to continue."
#: Seconds `close()` waits for background hooks and callbacks before cancelling them.
CLOSE_DRAIN_SECONDS = 5.0
#: Characters of a command's stdout kept as the record's output.
COMMAND_OUTPUT_CHARS = 4000
COMPACT_FAILURES = (
    "Compaction canceled.",
    "Not enough messages to compact.",
    "Error compacting conversation",
)
#: reload-plugins left the plugin cache as is because the MCP tool set would change; its
#: stdout ends "Run ... --force to apply." and the record's result is `skipped_cache`.
RELOAD_FORCE_HINT = "--force to apply"
#: Seconds a compact completed by the PostCompact relay waits for its transcript boundary, so
#: `compacted` is emitted before `command_completed`.
COMPACT_BOUNDARY_GRACE = 1.0


def _compact_done(sig: Signal) -> bool:
    if sig.kind in ("post_compact", "compact_boundary"):
        return sig.data.get("trigger") == "manual"
    return sig.kind == "command_output" and any(f in _output_text(sig) for f in COMPACT_FAILURES)


def _invoked(name: str) -> Callable[[Signal], bool]:
    return lambda sig: sig.kind == "command_invoked" and sig.data.get("name") == name


def _output_text(sig: Signal) -> str:
    parts = (str(sig.data.get("stdout") or ""), str(sig.data.get("stderr") or ""))
    return "\n".join(p for p in parts if p)[:COMMAND_OUTPUT_CHARS]


def _results_line(records: list[CommandRecord]) -> str:
    """`Command results: <name> <state>[ <result or error>]; ...` for the continue prompt."""
    parts = []
    for r in records:
        detail = r.result if r.state == "completed" else r.error
        parts.append(f"{r.name} {r.state}" + (f" {detail}" if detail else ""))
    return "Command results: " + ("; ".join(parts) if parts else "none")


def _abort_reason(record: TurnRecord) -> str:
    """Why the commands of a turn that ended in `record.state` fail."""
    if record.state in ("interrupted", "timeout"):
        return record.state
    if record.error in ("claude_exited", "session_stopped"):
        return record.error
    return "turn_failed"


class TurnVetoed(Exception):
    """An `onTurn` hook denied the turn."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class SessionInfo:
    """Identity of the session the router serves; `callback_url` is the session default."""

    session_id: str
    claude_session_id: str
    workdir: str
    callback_url: str | None = None

    @classmethod
    def coerce(cls, value: SessionInfo | Mapping[str, Any]) -> SessionInfo:
        if isinstance(value, SessionInfo):
            return value
        return cls(
            session_id=str(value["session_id"]),
            claude_session_id=str(value["claude_session_id"]),
            workdir=str(value["workdir"]),
            callback_url=value.get("callback_url") or value.get("callbackUrl"),
        )


def _attribution(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Which agent acted: Claude puts agent_id/agent_type on hooks fired inside subagents."""
    return {"agent_id": payload.get("agent_id"), "agent_type": payload.get("agent_type")}


def _allow() -> RelayResponse:
    return {"stdout": "", "stderr": "", "exit_code": 0}


def _additional_context(event_name: str, context: str) -> str:
    return json.dumps(
        {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": context}}
    )


def _block_output(reason: str) -> str:
    return json.dumps({"decision": "block", "reason": reason})


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _list_or_empty(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _response_text(payload: Mapping[str, Any]) -> str:
    response = payload.get("tool_response")
    if response is None:
        response = payload.get("error", "")
    text = (
        response
        if isinstance(response, str)
        else json.dumps(response, ensure_ascii=False, default=str)
    )
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + TRUNCATED_SUFFIX


def _response_is_error(event_name: str, payload: Mapping[str, Any]) -> bool:
    if event_name == "PostToolUseFailure" or payload.get("is_error"):
        return True
    response = payload.get("tool_response")
    return isinstance(response, dict) and bool(response.get("is_error"))


class SessionRouter(SessionListener):
    def __init__(
        self,
        session: Session | None,
        event_log: EventLog,
        dispatcher: HookDispatcher,
        redactor: Redactor,
        cfg: Config,
        *,
        session_info: SessionInfo | Mapping[str, Any],
        metrics: Metrics | None = None,
        commands: CommandsConfig | None = None,
    ) -> None:
        self._metrics = metrics
        self.commands_cfg = commands if commands is not None else cfg.sessions.commands
        self.commands = CommandQueue(self.commands_cfg)
        self.signals = SignalBus()
        self._session = session
        self._event_log = event_log
        self._dispatcher = dispatcher
        self._redactor = redactor
        self._cfg = cfg
        self.info = SessionInfo.coerce(session_info)
        self._usage = UsageTotals()
        self._last_marker: str | None = None
        self._tailer: TranscriptTailer | None = None
        self._watcher: SubagentWatcher | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._background: set[asyncio.Task[Any]] = set()
        self._dropping_history = False
        self._tick_tasks: list[asyncio.Task[Any]] = []
        self._tick_runs: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._subagents: set[str] = set()
        self._runner: asyncio.Task[Any] | None = None
        #: Why the runner is being cancelled (claude_exited, session_stopped, interrupted).
        self._abort: str | None = None
        #: The record the runner is working on, so an abort can fail it.
        self._current: CommandRecord | None = None
        #: The running turn whose Stop was answered while its slash commands run (D4).
        self._commands_turn: TurnRecord | None = None
        #: Idle auto-compact (D6): the pending timer, whether this idle gap may still fire, and
        #: the context size (input + cache tokens) of the last main-chain Usage.
        self._ac_handle: asyncio.TimerHandle | None = None
        self._ac_armed = False
        self._last_context: int | None = None
        #: Latest statusLine values relayed by `ccw statusline` (see `on_statusline`).
        self.statusline: dict[str, Any] | None = None
        #: Monotonic time of the last event that is not an idle-origin command's.
        self._last_real = time.monotonic()
        #: While idle-origin commands run: the seq of the first event and whether only idle
        #: commands ran. Their events are no activity (idle clock, reaper).
        self._idle_start: int | None = None
        self._idle_only = True
        #: Inclusive (first, last) event seq ranges of finished idle-only command runs.
        self._idle_spans: list[tuple[int, int]] = []
        #: The session's peer inbox (D7), set by the manager when peers are enabled.
        self.peer_inbox: PeerInbox | None = None
        # The dispatcher is built before the router; route its per-action results here.
        if getattr(dispatcher, "_on_result", None) is None:
            dispatcher._on_result = self.on_hook_result

    # ------------------------------------------------------------------ wiring

    def bind(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        if self._session is None:
            raise RuntimeError("router is not bound to a session")
        return self._session

    @property
    def event_log(self) -> EventLog:
        return self._event_log

    @property
    def turns(self) -> TurnStore | None:
        return self.session.turn_store

    @property
    def tailer(self) -> TranscriptTailer | None:
        return self._tailer

    @property
    def subagents(self) -> SubagentWatcher | None:
        return self._watcher

    def emit_event(self, type: str, data: Mapping[str, Any] | None = None) -> LogEvent:
        """Append a redacted event to this session's log (for collaborators such as the inbox)."""
        return self._emit(type, data)

    def _emit(self, type: str, data: Mapping[str, Any] | None = None) -> LogEvent:
        if self._idle_start is None or not self._idle_only:
            self._last_real = time.monotonic()
        return self._event_log.append(type, self._redactor.redact_obj(dict(data or {})))

    def _emit_user_message(
        self, turn_id: str | None, origin: str, text: str, files: list[str] | None = None
    ) -> None:
        """One event per prompt pasted into the session (client, peer, restart, command...)."""
        self._emit(
            "user_message",
            {"turn_id": turn_id, "origin": origin, "text": text, "files": files or []},
        )

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        task.add_done_callback(self._log_failure)
        return task

    @staticmethod
    def _log_failure(task: asyncio.Future[Any]) -> None:
        if not task.cancelled() and task.exception() is not None:
            log.error("router background task failed", exc_info=task.exception())

    async def drain(self, timeout: float | None = None) -> None:
        """Wait for background hook fires and callback deliveries, including those they start.

        A turn's `_after_turn` spawns its callback delivery only after onTurnComplete returns,
        so one snapshot of the pending tasks would let `close()` cancel that delivery.
        """
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while pending := [t for t in self._background if not t.done()]:
            left = None if deadline is None else deadline - loop.time()
            if left is not None and left <= 0:
                return
            await asyncio.wait(pending, timeout=left)

    async def _fire(
        self,
        event: str,
        data: Mapping[str, Any],
        *,
        tool_name: str | None = None,
        turn_id: str | None = None,
        only_index: int | None = None,
    ) -> HookOutcome:
        """Run `event`'s hooks; the envelope names `turn_id`, else the running turn."""
        try:
            if turn_id is None and (current := self.session.current_turn) is not None:
                turn_id = current.turn_id
            payload = build_payload(
                event,
                session_id=self.info.session_id,
                claude_session_id=self.info.claude_session_id,
                turn_id=turn_id,
                workdir=self.info.workdir,
                data=data,
            )
            return await self._dispatcher.fire(
                event, payload, tool_name=tool_name, only_index=only_index
            )
        except Exception:  # noqa: BLE001 - user hooks never break the router
            log.exception("firing %s failed", event)
            if event == "onAgentToolCall":
                raise  # the PreToolUse gate decides whether this fails closed
            return HookOutcome()

    def _report_error(self, kind: str, message: str, **extra: Any) -> dict[str, Any]:
        data = {"kind": kind, "message": self._redactor.redact(message), **extra}
        self._emit("error", data)
        return data

    def _annotate(self, record: TurnRecord | None, outcome: HookOutcome) -> None:
        """Merge a hook outcome's annotations into `record`; over 16 KiB is dropped and reported."""
        if record is None or not outcome.annotations:
            return
        if not self.session.annotate(record, outcome.annotations):
            self._report_annotations_too_large(record.turn_id)

    def _report_annotations_too_large(self, turn_id: str) -> None:
        self._report_error(
            "annotations_too_large",
            f"annotations for turn {turn_id} dropped: the merge would exceed 16 KiB",
            turn_id=turn_id,
        )

    def _callback_url(self, record: TurnRecord | None) -> str | None:
        if record is not None and record.callback_url:
            return record.callback_url
        return self.info.callback_url

    def _deliver(self, url: str | None, event: LogEvent) -> None:
        if not url:
            return
        secret = self._cfg.server.callback_secret or None
        self._spawn(self._dispatcher.post_json(url, asdict(event), secret=secret))

    # ------------------------------------------------------------------ tailing

    def ensure_tailing(self) -> None:
        """Run the transcript tailer and subagent watcher on the session's transcript path."""
        if self._closed:
            return
        path = Path(self.session.transcript_path)
        if self._tailer is None:
            self._start_tailing(path)
        elif self._tailer.path != path:
            self._tailer.switch(path)
            if self._watcher is not None:
                self._watcher.switch(path)

    def _start_tailing(self, path: Path) -> None:
        self._tailer = TranscriptTailer(path, self._on_transcript_events, redactor=self._redactor)
        self._watcher = SubagentWatcher(path, self._on_transcript_events, redactor=self._redactor)
        # A resumed session's transcript already holds its history: do not replay it as events.
        spec = getattr(self.session, "spec", None)
        self._dropping_history = bool(getattr(spec, "resume", False)) and path.exists()
        self._tasks.append(asyncio.ensure_future(self._watcher.run()))
        self._tasks.append(asyncio.ensure_future(self._run_tailer(self._tailer)))

    async def _run_tailer(self, tailer: TranscriptTailer) -> None:
        if self._dropping_history:
            try:
                await tailer.flush()
                if self._watcher is not None:
                    await self._watcher.flush()
            finally:
                self._dropping_history = False
        await tailer.run()

    async def _flush_transcripts(self) -> None:
        if self._tailer is not None:
            await self._tailer.flush()
        if self._watcher is not None:
            await self._watcher.flush()

    def _track_path(self, payload: Mapping[str, Any]) -> None:
        path = payload.get("transcript_path")
        if isinstance(path, str) and path and Path(path) != Path(self.session.transcript_path):
            self.session.transcript_path = path
        self.ensure_tailing()

    def on_statusline(self, payload: Mapping[str, Any]) -> None:
        """Store the latest context-window values and model from the statusLine relay."""
        self.statusline = dict(payload)

    # ------------------------------------------------------------------ claude hooks

    async def handle_claude_hook(
        self, event_name: str, payload: Mapping[str, Any]
    ) -> RelayResponse:
        try:
            self.session.note_activity()
            self._track_path(payload)
            handler = {
                "UserPromptSubmit": self._on_user_prompt_submit,
                "PreToolUse": self._on_pre_tool_use,
                "PostToolUse": self._on_post_tool_use,
                "PostToolUseFailure": self._on_post_tool_use,
                "Stop": self._on_stop,
                "SubagentStart": self._on_subagent,
                "SubagentStop": self._on_subagent,
                "PreCompact": self._on_compact_hook,
                "PostCompact": self._on_compact_hook,
            }.get(event_name)
            if handler is None:
                return _allow()
            return await handler(event_name, payload)
        except Exception as exc:  # noqa: BLE001 - claude is never broken by the router
            log.exception("hook relay %s failed", event_name)
            tool_name = str(payload.get("tool_name") or "")
            # an unknown tool name could match any gate: consider them all
            closed = (
                event_name == "PreToolUse"
                and gate_fail_mode(self._cfg.hooks, tool_name or None) == "deny"
            )
            response = self._fail_closed(exc, tool_name, payload) if closed else _allow()
            try:
                data = self._report_error(
                    "hook_relay", f"{event_name}: {type(exc).__name__}: {exc}"
                )
                self._spawn(self._fire("onError", data))
            except Exception:  # noqa: BLE001
                log.exception("reporting hook relay failure failed")
            return response

    async def _on_compact_hook(self, event_name: str, payload: Mapping[str, Any]) -> RelayResponse:
        """Compaction hooks only feed the signal bus; `compacted` comes from the boundary."""
        if event_name == "PreCompact":
            self.signals.publish(
                "pre_compact",
                {
                    "trigger": payload.get("trigger"),
                    "custom_instructions": payload.get("custom_instructions"),
                },
            )
        else:
            self.signals.publish(
                "post_compact",
                {
                    "trigger": payload.get("trigger"),
                    "compact_summary": payload.get("compact_summary"),
                },
            )
        return _allow()

    def _fail_closed(
        self, exc: Exception, tool_name: str, payload: Mapping[str, Any]
    ) -> RelayResponse:
        """Deny the tool call after a ccw-side failure; every step is best effort."""
        reason = f"ccw hook relay failed: {type(exc).__name__}"
        try:
            self._emit(
                "tool_denied",
                {
                    "tool_use_id": payload.get("tool_use_id"),
                    "tool_name": tool_name,
                    "reason": reason,
                    **_attribution(payload),
                },
            )
        except Exception:  # noqa: BLE001
            log.exception("emitting tool_denied failed")
        try:
            if self._metrics is not None:
                self._metrics.tool_call(tool_name, "deny")
        except Exception:  # noqa: BLE001
            log.exception("counting the denied tool call failed")
        return {"stdout": deny_output(reason), "stderr": "", "exit_code": 0}

    async def _on_user_prompt_submit(
        self, event_name: str, payload: Mapping[str, Any]
    ) -> RelayResponse:
        return _allow()

    async def _on_pre_tool_use(self, event_name: str, payload: Mapping[str, Any]) -> RelayResponse:
        tool_name = str(payload.get("tool_name") or "")
        tool_use_id = payload.get("tool_use_id")
        tool_input = payload.get("tool_input")
        if tool_input is None:
            tool_input = {}
        if tool_name == "AskUserQuestion" and self._cfg.sessions.ask_user_question == "forward":
            event = self._emit("question", {"tool_use_id": tool_use_id, "questions": tool_input})
            self._deliver(self._callback_url(self.session.current_turn), event)
            return {"stdout": deny_output(QUESTION_DENY_REASON), "stderr": "", "exit_code": 0}
        who = _attribution(payload)
        gate_reason = None
        if self._cfg.peers.enabled and tool_name in NATIVE_PEER_TOOLS:
            gate_reason = NATIVE_PEER_DENY_REASON
        elif tool_name.startswith(CCW_TOOL_PREFIX) and payload.get("agent_id"):
            gate_reason = CCW_SUBAGENT_DENY_REASON
        if gate_reason is not None:
            self._emit(
                "tool_denied",
                {
                    "tool_use_id": tool_use_id,
                    "tool_name": tool_name,
                    "reason": gate_reason,
                    **who,
                },
            )
            if self._metrics is not None:
                self._metrics.tool_call(tool_name, "deny")
            return {"stdout": deny_output(gate_reason), "stderr": "", "exit_code": 0}
        data = {
            "tool_use_id": tool_use_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
            **who,
        }
        self._emit("tool_call", data)
        outcome = await self._fire("onAgentToolCall", data, tool_name=tool_name)
        self._annotate(self.session.current_turn, outcome)
        if self._metrics is not None:
            self._metrics.tool_call(tool_name, "deny" if outcome.decision == "deny" else "allow")
        if outcome.decision == "deny":
            reason = outcome.reason or DEFAULT_DENY_REASON
            self._emit(
                "tool_denied",
                {"tool_use_id": tool_use_id, "tool_name": tool_name, "reason": reason, **who},
            )
            return {"stdout": deny_output(reason), "stderr": "", "exit_code": 0}
        if outcome.additional_context:
            return {
                "stdout": _additional_context(event_name, outcome.additional_context),
                "stderr": "",
                "exit_code": 0,
            }
        return _allow()

    async def _on_post_tool_use(self, event_name: str, payload: Mapping[str, Any]) -> RelayResponse:
        tool_name = str(payload.get("tool_name") or "")
        data = {
            "tool_use_id": payload.get("tool_use_id"),
            "tool_name": tool_name,
            "is_error": _response_is_error(event_name, payload),
            "response": _response_text(payload),
            **_attribution(payload),
        }
        self._emit("tool_result", data)
        self.session.record_tool_call()
        outcome = await self._fire("onToolResult", data, tool_name=tool_name)
        self._annotate(self.session.current_turn, outcome)
        if outcome.additional_context:
            return {
                "stdout": _additional_context(event_name, outcome.additional_context),
                "stderr": "",
                "exit_code": 0,
            }
        return _allow()

    async def _on_stop(self, event_name: str, payload: Mapping[str, Any]) -> RelayResponse:
        await self._flush_transcripts()
        held = await self._stop_gate(payload)
        if held is not None:
            return held
        per_model = self._usage.as_dict()
        usage: dict[str, int] | None = None
        if per_model:
            usage = {}
            for bucket in per_model.values():
                for name, count in bucket.items():
                    usage[name] = usage.get(name, 0) + count
        record = self.session.complete(
            dict(payload),
            usage=usage,
            model_tokens=per_model or None,
            marker_turn_id=self._last_marker,
        )
        self._usage.reset()
        if self._metrics is not None:
            self._metrics.tokens(per_model)
        if record is not None:
            if self._metrics is not None:
                self._metrics.turn_finished(record)
            event = self._emit_turn_event("turn_completed", record)
            await self._after_turn(record, event)
        return _allow()

    async def _stop_gate(self, payload: Mapping[str, Any]) -> RelayResponse | None:
        """onStop for a Stop that would end the running turn; the relay answer if it stays open.

        None means "complete as today": no gate is configured and no command is queued, the
        Stop does not target the running turn (stale, foreign, or a background completion while
        ready), or the gate allowed it with nothing queued. A block sends claude back to work
        with the reason, a defer answers claude allow; either way the turn stays running and
        busy, its usage keeps accumulating and queued commands stay queued. Queued commands
        after an allow: answer allow and run them once this answer is back with claude (D4.2);
        the turn then resumes with the continue prompt.
        """
        sessions = self._cfg.sessions
        if not (
            self._cfg.hooks.onStop
            or sessions.defer_stop_on_background_tasks
            or self.commands.pending()
            or self._commands_turn is not None
        ):
            return None
        session = self.session
        record = session.stop_target(payload, self._last_marker)
        if record is None or record is not session.current_turn:
            return None
        if record is self._commands_turn:
            return _allow()  # a background Stop while the commands run: the turn goes on
        tasks = _list_or_empty(payload.get("background_tasks"))
        active = bool(payload.get("stop_hook_active"))
        data = {
            "turn_id": record.turn_id,
            "stop_hook_active": active,
            "last_assistant_message": _str_or_none(payload.get("last_assistant_message")),
            "background_tasks": tasks,
            "session_crons": _list_or_empty(payload.get("session_crons")),
            "transcript_path": _str_or_none(payload.get("transcript_path")),
        }
        # The built-in defer is decided first; a hook allow cannot lift it, a hook block wins.
        builtin_defer = sessions.defer_stop_on_background_tasks and bool(tasks)
        outcome = await self._fire("onStop", data, turn_id=record.turn_id)
        self._annotate(record, outcome)  # every leg: allow, block and defer
        if record.terminal:
            return _allow()  # the turn ended (interrupt, timeout, exit) while the gate ran
        if outcome.decision == "block":
            reason = outcome.reason or DEFAULT_BLOCK_REASON
            self._emit(
                "turn_continued",
                {"turn_id": record.turn_id, "reason": reason, "stop_hook_active": active},
            )
            return {"stdout": _block_output(reason), "stderr": "", "exit_code": 0}
        commands = [r.id for r in self.commands.pending()]
        if outcome.decision == "defer" or builtin_defer:
            reason = "hook" if outcome.decision == "defer" else "background_tasks"
            self._emit_deferred(record, reason, tasks, commands)
            return _allow()
        if not commands or not session.hold_for_commands(record):
            return None
        self._commands_turn = record
        self._emit_deferred(record, "command", tasks, commands)
        # Started on the next loop pass: after this handler returned the answer to the relay.
        asyncio.get_running_loop().call_soon(self._start_turn_runner, record)
        return _allow()

    def _emit_deferred(
        self, record: TurnRecord, reason: str, tasks: list[Any], commands: list[str]
    ) -> None:
        self._emit(
            "turn_deferred",
            {
                "turn_id": record.turn_id,
                "reason": reason,
                "background_tasks": tasks,
                "commands": commands,
            },
        )

    @property
    def outstanding_subagents(self) -> int:
        """Subagents seen in SubagentStart and not yet in SubagentStop."""
        return len(self._subagents)

    async def _on_subagent(self, event_name: str, payload: Mapping[str, Any]) -> RelayResponse:
        started = event_name == "SubagentStart"
        agent_id = payload.get("agent_id")
        if isinstance(agent_id, str):
            if started:
                self._subagents.add(agent_id)
            else:
                self._subagents.discard(agent_id)
        data: dict[str, Any] = {
            "agent_id": payload.get("agent_id"),
            "agent_type": payload.get("agent_type"),
        }
        if not started and isinstance(payload.get("last_assistant_message"), str):
            data["last_assistant_message"] = payload["last_assistant_message"]
        self._emit("subagent_started" if started else "subagent_stopped", data)
        await self._fire("onSubagentStart" if started else "onSubagentStop", data)
        return _allow()

    # ------------------------------------------------------------------ turns

    def _emit_turn_event(self, type: str, record: TurnRecord) -> LogEvent:
        return self._emit(type, {"record": record.model_dump(mode="json", by_alias=True)})

    async def _after_turn(self, record: TurnRecord, event: LogEvent) -> None:
        """onTurnComplete (any terminal state) and the callback delivery."""
        if self.peer_inbox is not None:
            self.peer_inbox.turn_finished(record)  # synchronous: never delays the Stop answer
        self._arm_auto_compact()
        outcome = await self._fire(
            "onTurnComplete", record.model_dump(mode="json", by_alias=True), turn_id=record.turn_id
        )
        self._annotate(record, outcome)  # persisted again; the callback below is not re-sent
        self._deliver(self._callback_url(record), event)

    async def submit_turn(
        self,
        text: str,
        *,
        files: Mapping[str, str] | None = None,
        callback_url: str | None = None,
        origin: Origin = "client",
        metadata: Mapping[str, Any] | None = None,
    ) -> TurnRecord:
        self.session.check_submittable()  # a doomed turn fires no hooks
        was_armed = self._ac_armed
        self._cancel_auto_compact()
        try:
            return await self._submit_turn(text, files, callback_url, origin, metadata)
        except BaseException:
            if was_armed:  # no turn started and none will end: keep the idle gap alive
                self._arm_auto_compact()
            raise

    async def _submit_turn(
        self,
        text: str,
        files: Mapping[str, str] | None,
        callback_url: str | None,
        origin: Origin,
        metadata: Mapping[str, Any] | None,
    ) -> TurnRecord:
        turn_id = new_turn_id()
        outcome = await self._fire(
            "onTurn",
            {
                "text": text,
                "files": list((files or {}).keys()),
                "origin": origin,
                "metadata": dict(metadata or {}),
            },
            turn_id=turn_id,
        )
        if outcome.decision == "deny":
            reason = outcome.reason or DEFAULT_DENY_REASON
            self._emit("turn_rejected", {"reason": reason})
            raise TurnVetoed(reason)
        # No await from this check through safe_write_files to submit's slot reservation.
        self.session.check_submittable()
        if files:
            safe_write_files(
                self.info.workdir,
                files,
                max_bytes=self._cfg.limits.max_file_bytes,
                max_files=self._cfg.limits.max_files_per_turn,
            )
        annotations = outcome.annotations
        if annotations and merged_annotations({}, annotations) is None:
            self._report_annotations_too_large(turn_id)
            annotations = {}
        record = await self.session.submit(
            text,
            origin=origin,
            callback_url=callback_url,
            turn_id=turn_id,
            annotations=annotations,
            metadata=dict(metadata or {}),
        )
        self._usage.reset()
        self._emit_user_message(record.turn_id, record.origin, text, list((files or {}).keys()))
        self._emit(
            "turn_started", {"turn_id": record.turn_id, "origin": record.origin, "text": text}
        )
        self.ensure_tailing()
        return record

    async def interrupt(self) -> str | None:
        session = self.session
        if session.command_active and self.runner_active:
            return await self._interrupt_command()
        runner = self._runner if self.runner_active else None
        turn_id = await session.interrupt()
        if runner is not None and turn_id is not None:
            # The turn's commands: on_turn_end aborted the runner; let it settle the records.
            await asyncio.wait({runner}, timeout=CLOSE_DRAIN_SECONDS)
        if self._metrics is not None:
            self._metrics.interrupt("idle" if turn_id is None else "interrupted")
        if turn_id is not None:
            self._emit("interrupted", {"turn_id": turn_id})
            await self._fire("onInterrupt", {"turn_id": turn_id}, turn_id=turn_id)
        return turn_id

    async def _interrupt_command(self) -> None:
        """Idle session, a command running: ESC, the command fails `interrupted`, queued ones
        are cancelled, the session returns to ready."""
        runner = self._abort_runner("interrupted")
        with contextlib.suppress(PtyClosed, TurnRejected):
            await self.session.send_escape()
        if runner is not None:
            await asyncio.gather(runner, return_exceptions=True)
        if self._metrics is not None:
            self._metrics.interrupt("interrupted")

    # ------------------------------------------------------------------ transcript events

    async def _on_transcript_events(self, events: list[TranscriptEvent]) -> None:
        self.session.note_activity()
        if self._dropping_history:
            return
        for ev in events:
            if isinstance(ev, AssistantText):
                await self._on_assistant_text(ev)
            elif isinstance(ev, Thinking):
                self._emit(
                    "thinking",
                    {"text": ev.text, "message_id": ev.message_id, "agent_id": ev.agent_id},
                )
            elif isinstance(ev, Usage):
                if not ev.sidechain and ev.agent_id is None:
                    self._last_context = (
                        ev.input_tokens
                        + ev.cache_read_input_tokens
                        + ev.cache_creation_input_tokens
                    )
                if self._usage.add(ev):
                    self._emit(
                        "usage",
                        {
                            "message_id": ev.message_id,
                            "model": ev.model,
                            "agent_id": ev.agent_id,
                            "input_tokens": ev.input_tokens,
                            "output_tokens": ev.output_tokens,
                            "cache_read_input_tokens": ev.cache_read_input_tokens,
                            "cache_creation_input_tokens": ev.cache_creation_input_tokens,
                        },
                    )
            elif isinstance(ev, Interrupted):
                if not ev.sidechain:
                    self.session.on_interrupted()
            elif isinstance(ev, CompactBoundary):
                if not ev.sidechain:
                    data = {
                        "trigger": ev.trigger,
                        "pre_tokens": ev.pre_tokens,
                        "post_tokens": ev.post_tokens,
                        "duration_ms": ev.duration_ms,
                    }
                    self._emit("compacted", data)
                    if self._metrics is not None:
                        self._metrics.compaction(ev.trigger)
                    self.signals.publish("compact_boundary", data)
            elif isinstance(ev, CommandInvoked):
                if not ev.sidechain:
                    self.signals.publish("command_invoked", {"name": ev.name, "args": ev.args})
            elif isinstance(ev, CommandOutput):
                if not ev.sidechain:
                    self.signals.publish(
                        "command_output", {"stdout": ev.stdout, "stderr": ev.stderr}
                    )
            elif isinstance(ev, UserPrompt):
                if ev.turn_marker:
                    self._last_marker = ev.turn_marker
            elif isinstance(ev, ProbeMarker):
                probe = self.session.probe_delivered(ev.probe_id)
                if probe is not None and self._metrics is not None:
                    self._metrics.probe_outcome("delivered")

    async def _on_assistant_text(self, ev: AssistantText) -> None:
        self._emit(
            "assistant_text",
            {
                "text": ev.text,
                "message_id": ev.message_id,
                "model": ev.model,
                "agent_id": ev.agent_id,
            },
        )
        if ev.sidechain:
            return
        if self._cfg.sessions.probe_alive_token in ev.text:
            for probe in self.session.probe_answered(ev.text):
                if self._metrics is not None:
                    self._metrics.probe_answered(probe)
                self._emit("probe", probe.model_dump(mode="json"))
        await self._fire(
            "onAgentResponse",
            {"text": ev.text, "message_id": ev.message_id, "model": ev.model},
        )

    # ------------------------------------------------------------------ slash commands

    def request_command(
        self,
        name: str,
        params: Mapping[str, Any] | None = None,
        *,
        origin: CommandOrigin,
    ) -> CommandRecord:
        """Queue an allowlisted slash command; `CommandRejected` propagates unchanged.

        A new record emits `command_queued`; a coalesced duplicate returns the queued record and
        emits nothing. On a ready session with no turn the runner starts at once (idle path);
        while a turn runs the command stays queued for that turn's Stop.
        """
        validate_params(name, params, allow_force=self.commands_cfg.allow_force)
        if self.session.state not in ("ready", "busy"):
            raise CommandRejected("invalid_state", f"session is {self.session.state}")
        turn = self.session.current_turn
        record, new = self.commands.request(
            name, params, origin=origin, turn_id=turn.turn_id if turn is not None else None
        )
        if origin != "idle":  # accepted: a rejected request never started anything
            self._cancel_auto_compact()
            self._idle_only = False  # from here on the events count as activity
        if new:
            if record.origin == "idle" and self._idle_start is None:
                self._idle_only = True
                self._idle_start = self._emit_command("command_queued", record).seq
            else:
                self._emit_command("command_queued", record)
        self._start_idle_runner()
        return record

    @property
    def runner_active(self) -> bool:
        """A command runner is working through the queue (at most one per session)."""
        return self._runner is not None and not self._runner.done()

    async def wait_runner_idle(self) -> None:
        """Return once no command runner is active."""
        while (runner := self._runner) is not None and not runner.done():
            await asyncio.wait({runner})

    def _start_idle_runner(self) -> bool:
        """Start the runner on an idle session (D4.8); False when it cannot start now."""
        if self._closed or self.runner_active or self.commands.next() is None:
            return False
        session = self.session
        if session.current_turn is not None or not session.begin_command():
            return False
        self._runner = asyncio.ensure_future(self._run_idle())
        self._runner.add_done_callback(self._log_failure)
        return True

    def _start_turn_runner(self, record: TurnRecord) -> None:
        """Run the queue for the running turn `record` whose Stop was answered (D4.3)."""
        if self._closed or record.terminal or record is not self.session.current_turn:
            if self._commands_turn is record:
                self._commands_turn = None
            return
        previous = self._runner if self.runner_active else None
        self._runner = asyncio.ensure_future(self._run_turn(record, previous))
        self._runner.add_done_callback(self._log_failure)

    async def _run_turn(
        self, record: TurnRecord, previous: asyncio.Task[Any] | None = None
    ) -> None:
        """Run every queued command, then resume `record` with the continue prompt (D4.4).

        An abort (interrupt, turn_max_seconds, crash, stop) ESCs the command being typed or
        awaited when claude still lives, fails it, cancels the queued ones and resumes nothing.
        `previous` is a runner still finishing (one at a time); this one waits for it.
        """
        try:
            if previous is not None:
                await asyncio.wait({previous})
            # The Stop answer travels back to claude; claude repaints its prompt meanwhile.
            await asyncio.sleep(self.commands_cfg.quiet_seconds)
            done = await self._run_queue()
            await self._resume_turn(record, done)
        except asyncio.CancelledError:
            reason = self._abort
            if reason is None:
                raise
            current = self._current
            if reason in ("interrupted", "timeout") and current and current.state == "running":
                with contextlib.suppress(PtyClosed, TurnRejected):
                    await self.session.send_escape()
            await self._fail_commands(reason)
        finally:
            self._abort = None
            self._current = None
            if self._commands_turn is record:
                self._commands_turn = None

    async def _resume_turn(self, record: TurnRecord, done: list[CommandRecord]) -> None:
        session = self.session
        if done and done[-1].error == "claude_exited":
            return  # claude died under a command: K12 decides the turn
        # Typed at a quiet prompt, like a command; a screen that never settles still gets it
        # (claude queues input typed while it responds) rather than leaving the turn hanging.
        quiet = self.commands_cfg.quiet_seconds
        await session.wait_quiet(quiet, self._command_timeout("default"))
        if self._closed or record.terminal or record is not session.current_turn:
            return
        self._commands_turn = None  # from here on a Stop is the continuation's
        ids = [r.id for r in done]
        text = f"{self.commands_cfg.continue_prompt}\n\n{_results_line(done)}"
        self._last_marker = record.turn_id  # the continuation's Stop completes this record
        self._emit("turn_resumed", {"turn_id": record.turn_id, "cause": "command", "commands": ids})
        if await session.continue_turn(record, text):
            self._emit_user_message(record.turn_id, record.origin, text)

    def _end_turn_commands(self, record: TurnRecord) -> None:
        """A turn ended without completing: its commands never resume it (D4.5-D4.7).

        The runner working for it is aborted (the running command fails, the queued ones are
        cancelled); without a runner its queued commands are cancelled at once, before anything
        can start them on the now ready session.
        """
        if self._commands_turn is record:
            self._commands_turn = None
            if self.runner_active:
                self._abort_runner(_abort_reason(record))
                return
        started = time.monotonic()
        for rec in self.commands.pending():
            if rec.turn_id == record.turn_id and self._settle_command(rec, "cancelled", started):
                self._spawn(self._fire_command_complete(rec))

    async def _run_idle(self) -> None:
        try:
            await self._run_queue()
        except asyncio.CancelledError:
            reason = self._abort
            if reason is None:
                raise
            await self._fail_commands(reason)
        finally:
            self._abort = None
            self._current = None
            self.session.end_command()  # no await since the queue was found empty
            self._close_idle_window()

    def _close_idle_window(self) -> None:
        """The idle runner is done: its events (up to the ready state) become an idle span."""
        start, self._idle_start = self._idle_start, None
        last = self._event_log.last(1)
        if start is not None and self._idle_only and last:
            self._idle_spans = [*self._idle_spans[-49:], (start, last[0].seq)]

    def idle_ranges(self) -> list[tuple[int, int]]:
        """Event seq ranges (inclusive) that are idle-origin command activity, not real activity."""
        ranges = list(self._idle_spans)
        if self._idle_start is not None and self._idle_only:
            ranges.append((self._idle_start, sys.maxsize))
        return ranges

    # ------------------------------------------------------------------ idle auto-compact

    def _arm_auto_compact(self) -> None:
        """A turn ended: fire once after `idle_seconds` without activity (D6)."""
        idle = self._cfg.sessions.auto_compact.idle_seconds
        self._cancel_auto_compact()
        if idle <= 0 or self._closed:
            return
        self._ac_armed = True
        self._ac_handle = asyncio.get_running_loop().call_later(idle, self._auto_compact_due)

    def _cancel_auto_compact(self) -> None:
        self._ac_armed = False
        if self._ac_handle is not None:
            self._ac_handle.cancel()
            self._ac_handle = None

    def _auto_compact_due(self) -> None:
        self._ac_handle = None
        cfg = self._cfg.sessions.auto_compact
        if not self._ac_armed or self._closed:
            return
        remaining = cfg.idle_seconds - (time.monotonic() - self._last_real)
        if remaining > 0.005:  # activity since arming: the gap restarts at the last event
            self._ac_handle = asyncio.get_running_loop().call_later(
                remaining, self._auto_compact_due
            )
            return
        self._ac_armed = False  # at most once per idle gap
        session = self.session
        if (
            session.state != "ready"
            or session.current_turn is not None
            or self.runner_active
            or self.commands.next() is not None
        ):
            return
        floor = cfg.min_context_tokens
        if floor > 0 and (self._last_context is None or self._last_context < floor):
            log.debug("auto-compact skipped: context %s below %s tokens", self._last_context, floor)
            return
        params = {"instructions": cfg.instructions} if cfg.instructions else {}
        try:
            self.request_command("compact", params, origin="idle")
        except CommandRejected as exc:
            log.warning("auto-compact not queued: %s", exc)

    def _abort_runner(self, reason: str) -> asyncio.Task[Any] | None:
        """Cancel the running runner; it fails the in-flight command with `reason` and cancels
        the queued ones. The first reason wins."""
        runner = self._runner
        if runner is None or runner.done():
            return None
        if self._abort is None:
            self._abort = reason
            runner.cancel()
        return runner

    async def _fail_commands(self, reason: str) -> None:
        """The in-flight command fails with `reason`; every queued one is cancelled."""
        started = time.monotonic()
        current = self._current
        if current is not None:
            await self._finish_quietly(current, "failed", started, error=reason)
        for record in self.commands.pending():
            await self._finish_quietly(record, "cancelled", started)

    async def _finish_quietly(
        self, record: CommandRecord, state: Any, started: float, *, error: str | None = None
    ) -> None:
        try:
            await self._finish_command(record, state, started, error=error)
        except Exception:  # noqa: BLE001 - one failing hook never leaves the slot held
            log.exception("command %s: finishing as %s failed", record.id, state)

    async def _run_queue(self) -> list[CommandRecord]:
        """Run every queued command in order, one at a time; the records run."""
        done: list[CommandRecord] = []
        while (record := self.commands.next()) is not None:
            await self._run_command(record)
            done.append(record)
            if record.error == "claude_exited":
                # Nothing more reaches claude: the exit path relaunches or ends the session.
                started = time.monotonic()
                for rest in self.commands.pending():
                    await self._finish_quietly(rest, "cancelled", started)
                break
        return done

    def _emit_command(self, type: str, record: CommandRecord) -> LogEvent:
        return self._emit(type, {"record": record.model_dump(mode="json")})

    def _command_timeout(self, name: str) -> float:
        timeouts = self.commands_cfg.timeout_seconds
        return float(timeouts.get(name, timeouts.get("default", 60)))

    async def _run_command(self, record: CommandRecord) -> None:
        """D4 step 3: quiet screen, onCommand, type the line, wait for completion or timeout."""
        session = self.session
        timeout = self._command_timeout(record.name)
        started = time.monotonic()
        self._current = record
        try:
            if not await session.wait_quiet(self.commands_cfg.quiet_seconds, timeout):
                await self._finish_command(
                    record, "failed", started, error="timeout", output="the screen never settled"
                )
                return
            if record.state != "queued":
                return  # cancelled while the screen settled
            outcome = await self._fire(
                "onCommand",
                {
                    "command_id": record.id,
                    "name": record.name,
                    "args": dict(record.args),
                    "origin": record.origin,
                    "turn_id": record.turn_id,
                },
                turn_id=record.turn_id,
            )
            if outcome.decision == "deny":
                reason = outcome.reason or DEFAULT_DENY_REASON
                await self._finish_command(record, "failed", started, error="vetoed", output=reason)
                return
            if record.state != "queued":
                return  # cancelled while onCommand ran
            self.commands.start(record)
            started = time.monotonic()
            self._emit_command("command_started", record)
            mark = self.signals.mark()
            line = render(record.name, record.args)
            await session.send_command(line)
            self._emit_user_message(None, "command", line)
            state, result, error, output = await self._await_completion(record, mark, timeout)
            if error == "timeout":
                await session.send_escape()
        except (PtyClosed, TurnRejected):
            state, result, error, output = "failed", None, "claude_exited", None
        except Exception as exc:  # noqa: BLE001 - one command never wedges the queue
            log.exception("command %s failed", record.id)
            self._report_error("command", f"/{record.name}: {type(exc).__name__}: {exc}")
            state, result, error, output = "failed", None, "internal_error", None
        await self._finish_command(
            record, state, started, result=result, error=error, output=output
        )

    async def _await_completion(
        self, record: CommandRecord, mark: int, timeout: float
    ) -> tuple[str, str | None, str | None, str | None]:
        """(state, result, error, output) from the signal bus, or a timeout failure."""
        deadline = time.monotonic() + timeout

        def left() -> float:
            return max(deadline - time.monotonic(), 0.0)

        if record.name == "compact":
            sig = await self.signals.wait(_compact_done, after=mark, timeout=left())
            if sig is None:
                return "failed", None, "timeout", None
            if sig.kind == "command_output":
                return "failed", None, "compact_failed", _output_text(sig)
            if sig.kind == "post_compact":
                await self._await_compact_boundary(mark, min(left(), COMPACT_BOUNDARY_GRACE))
            return "completed", "ok", None, None
        invoked = await self.signals.wait(_invoked(record.name), after=mark, timeout=left())
        if invoked is None:
            return "failed", None, "timeout", None
        sig = await self.signals.wait(
            lambda s: s.kind == "command_output", after=invoked.seq, timeout=left()
        )
        if sig is None:
            return "failed", None, "timeout", None
        stdout = str(sig.data.get("stdout") or "")
        stderr = str(sig.data.get("stderr") or "")
        if not stdout and stderr:
            return "failed", None, "command_error", stderr[:COMMAND_OUTPUT_CHARS]
        result = (
            "skipped_cache"
            if record.name == "reload-plugins" and RELOAD_FORCE_HINT in stdout
            else "ok"
        )
        return "completed", result, None, stdout[:COMMAND_OUTPUT_CHARS]

    async def _await_compact_boundary(self, mark: int, timeout: float) -> None:
        """Deliver the compaction's transcript boundary (so `compacted`) before completion.

        The PostCompact relay can outrun the tailer's poll: flush first, then wait briefly for a
        boundary that is still being written. claude without a boundary completes regardless.
        """
        tailer = self._tailer
        if tailer is not None:
            with contextlib.suppress(OSError):
                await tailer.flush()
        await self.signals.wait(lambda s: s.kind == "compact_boundary", after=mark, timeout=timeout)

    async def _finish_command(
        self,
        record: CommandRecord,
        state: Any,
        started: float,
        *,
        result: str | None = None,
        error: str | None = None,
        output: str | None = None,
    ) -> None:
        """D4 step 6: final event, metric, onCommandComplete."""
        if self._settle_command(record, state, started, result=result, error=error, output=output):
            await self._fire_command_complete(record)

    def _settle_command(
        self,
        record: CommandRecord,
        state: Any,
        started: float,
        *,
        result: str | None = None,
        error: str | None = None,
        output: str | None = None,
    ) -> bool:
        """Finish `record` with its final event and metric; False when it already finished."""
        if record.state in ("completed", "failed", "cancelled"):
            return False
        self.commands.finish(record, state, result=result, error=error, output=output)
        self._emit_command(
            "command_completed" if state == "completed" else "command_failed", record
        )
        if self._metrics is not None:
            self._metrics.command_finished(
                record.name, record.origin, state, time.monotonic() - started
            )
        return True

    async def _fire_command_complete(self, record: CommandRecord) -> None:
        await self._fire(
            "onCommandComplete", record.model_dump(mode="json"), turn_id=record.turn_id
        )

    # ------------------------------------------------------------------ onTick timers

    def _start_ticks(self) -> None:
        """Start one timer per onTick action, once; claude restarts keep the same timers."""
        if self._tick_tasks or self._closed:
            return
        for index, action in enumerate(self._cfg.hooks.onTick):
            self._tick_tasks.append(asyncio.ensure_future(self._tick_loop(index, action)))

    async def _tick_loop(self, index: int, action: Any) -> None:
        interval = float(action.interval_seconds)
        busy_only = (action.when or "busy") == "busy"
        running: asyncio.Task[Any] | None = None
        while True:
            await asyncio.sleep(interval)
            if running is not None and not running.done():
                continue  # the previous run has not finished: skip this tick
            session = self.session
            turn = session.current_turn
            if busy_only and turn is None:
                continue
            data = {
                "turn_id": turn.turn_id if turn is not None else None,
                "elapsed_seconds": session.turn_elapsed,
                "interval_seconds": interval,
            }
            running = self._spawn(self._fire("onTick", data, only_index=index))
            self._tick_runs.add(running)
            running.add_done_callback(self._tick_runs.discard)

    # ------------------------------------------------------------------ SessionListener

    def on_state(self, state: str) -> None:
        self._emit(
            "session_state",
            {"state": state, "since": self.session.state_since or now_iso()},
        )
        internal = self.session.state
        if internal in ("booting", "dead"):
            self._abort_runner("claude_exited")
        elif internal in ("stopping", "stopped"):
            self._abort_runner("session_stopped")
        if state in ("ready", "busy"):
            with contextlib.suppress(RuntimeError):
                self.ensure_tailing()
            self._start_ticks()
        if state == "ready" and any(r.turn_id is None for r in self.commands.pending()):
            # Queued while no turn could take it (booting, a relaunch). Started on the next loop
            # pass, never from inside this state callback, so listeners see ready before busy.
            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop().call_soon(self._start_idle_runner)
        if state == "ready" and self.peer_inbox is not None:
            # After the idle runner's own scheduling, so a queued command takes the slot first.
            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop().call_soon(self.peer_inbox.kick)

    def on_fatal(self, text: str, tail: str) -> None:
        data = self._report_error("fatal", text, tail=self._redactor.redact(tail))
        self._spawn(self._fire("onError", data))

    def on_restart(self, count: int, resumed: bool) -> None:
        self._subagents.clear()
        self._emit(
            "session_state",
            {
                "state": self.session.snapshot()["state"],
                "since": self.session.state_since or now_iso(),
                "restart": {"count": count, "resumed": resumed},
            },
        )
        if self._metrics is not None:
            self._metrics.claude_restart()
        message = f"claude restarted (restart {count}, {'resumed' if resumed else 'fresh'})"
        self._spawn(self._fire("onError", {"kind": "restart", "message": message, "count": count}))

    def on_exit(self, code: int) -> None:
        data = self._report_error("exited", f"claude exited with code {code}", code=code)
        self._spawn(self._fire("onError", data))

    def on_stall(self, turn_id: str) -> None:
        if self._metrics is not None:
            self._metrics.stall_suspected()
        self._emit("stall_suspected", {"turn_id": turn_id})

    def on_turn_resumed(self, record: Any, restart: int) -> None:
        # The continue prompt carries this turn's marker, so its Stop completes the same record;
        # the usage totals are not reset, so they span the crash.
        self._last_marker = record.turn_id
        if self._commands_turn is record:
            self._commands_turn = None  # the crash aborted its commands
        self._emit(
            "turn_resumed", {"turn_id": record.turn_id, "restart": restart, "cause": "restart"}
        )
        self._emit_user_message(
            record.turn_id, "restart", self._cfg.sessions.restart_continue_prompt
        )

    def on_turn_end(self, record: Any) -> None:
        # Completed turns are finished by the Stop hook handler, which also has the usage.
        if record.state == "completed" or record.state not in TERMINAL_STATES:
            return
        self._end_turn_commands(record)
        if self._metrics is not None:
            self._metrics.turn_finished(record)
            if record.state == "timeout":
                self._metrics.interrupt("timeout")
        event = self._emit_turn_event("turn_failed", record)
        self._spawn(self._after_turn(record, event))

    def on_hook_result(self, result: ActionResult) -> None:
        if self._metrics is not None:
            self._metrics.hook_action(result.event, result.type, result.ok)
        data: dict[str, Any] = {
            "event": result.event,
            "index": result.index,
            "type": result.type,
            "ok": result.ok,
            "decision": result.decision,
            "duration_ms": result.duration_ms,
            "error": result.error,
        }
        if result.invalid_decision is not None:
            data["invalid_decision"] = result.invalid_decision
        if result.output is not None:
            data["output"] = result.output
        self._emit("hook_result", data)

    # ------------------------------------------------------------------ lifecycle

    async def close(
        self, drain_timeout: float = CLOSE_DRAIN_SECONDS, *, emit_closed: bool = True
    ) -> None:
        """Stop tailing, wait up to `drain_timeout` for background fires, then cancel them.

        `emit_closed=False` leaves `session_closed` to the caller (the manager emits it
        after onDestroy)."""
        if self._closed:
            return
        with contextlib.suppress(Exception):
            await self._flush_transcripts()
        self._closed = True
        self._cancel_auto_compact()
        if self.peer_inbox is not None:
            self.peer_inbox.close()  # nothing more is delivered; dropped before session_closed
        self._subagents.clear()
        runner = self._abort_runner("session_stopped")
        if runner is not None:
            await asyncio.gather(runner, return_exceptions=True)
        if self._tailer is not None:
            self._tailer.stop()
        if self._watcher is not None:
            self._watcher.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        runs = list(self._tick_runs)
        for task in [*self._tick_tasks, *runs]:
            task.cancel()
        await asyncio.gather(*self._tick_tasks, *runs, return_exceptions=True)
        await self.drain(max(drain_timeout, 0.0))
        for task in list(self._background):
            task.cancel()
        if emit_closed:
            self._emit("session_closed", {})
