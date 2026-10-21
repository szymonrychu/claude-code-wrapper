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
from collections.abc import Coroutine, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .events import Event as LogEvent
from .events import EventLog
from .hook_relay import RelayResponse, deny_output
from .hooks import ActionResult, HookDispatcher, HookOutcome, build_payload, gate_fail_mode
from .metrics import Metrics
from .redact import Redactor
from .session import Session, SessionListener
from .transcript import (
    MAX_RESULT_CHARS,
    TRUNCATED_SUFFIX,
    AssistantText,
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

log = logging.getLogger(__name__)

QUESTION_DENY_REASON = (
    "Question forwarded to the API client. Ask the user in plain text, "
    "end your turn now and wait for their reply in the next message."
)
DEFAULT_DENY_REASON = "Denied by a wrapper hook."
DEFAULT_BLOCK_REASON = "A wrapper onStop hook asked you to continue."
#: Seconds `close()` waits for background hooks and callbacks before cancelling them.
CLOSE_DRAIN_SECONDS = 5.0


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
    ) -> None:
        self._metrics = metrics
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

    def _emit(self, type: str, data: Mapping[str, Any] | None = None) -> LogEvent:
        return self._event_log.append(type, self._redactor.redact_obj(dict(data or {})))

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
            }.get(event_name)
            if handler is None:
                return _allow()
            return await handler(event_name, payload)
        except Exception as exc:  # noqa: BLE001 - claude is never broken by the router
            log.exception("hook relay %s failed", event_name)
            tool_name = str(payload.get("tool_name") or "")
            closed = (
                event_name == "PreToolUse" and gate_fail_mode(self._cfg.hooks, tool_name) == "deny"
            )
            try:
                data = self._report_error(
                    "hook_relay", f"{event_name}: {type(exc).__name__}: {exc}"
                )
                await self._fire("onError", data)
            except Exception:  # noqa: BLE001
                log.exception("reporting hook relay failure failed")
            if closed:
                return self._fail_closed(exc, tool_name, payload)
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

        None means "complete as today": no gate is configured, the Stop does not target the
        running turn (stale, foreign, or a background completion while ready), or the gate
        allowed it. A block sends claude back to work with the reason, a defer answers claude
        allow; either way the turn stays running and busy and its usage keeps accumulating.
        """
        sessions = self._cfg.sessions
        if not (self._cfg.hooks.onStop or sessions.defer_stop_on_background_tasks):
            return None
        session = self.session
        record = session.stop_target(payload, self._last_marker)
        if record is None or record is not session.current_turn:
            return None
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
        if outcome.decision == "defer" or builtin_defer:
            self._emit("turn_deferred", {"turn_id": record.turn_id, "background_tasks": tasks})
            return _allow()
        return None

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
        self._emit(
            "turn_started", {"turn_id": record.turn_id, "origin": record.origin, "text": text}
        )
        self.ensure_tailing()
        return record

    async def interrupt(self) -> str | None:
        turn_id = await self.session.interrupt()
        if turn_id is not None:
            self._emit("interrupted", {"turn_id": turn_id})
            await self._fire("onInterrupt", {"turn_id": turn_id}, turn_id=turn_id)
        return turn_id

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
            elif isinstance(ev, UserPrompt):
                if ev.turn_marker:
                    self._last_marker = ev.turn_marker
            elif isinstance(ev, ProbeMarker):
                self.session.probe_delivered(ev.probe_id)

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
                self._emit("probe", probe.model_dump(mode="json"))
        await self._fire(
            "onAgentResponse",
            {"text": ev.text, "message_id": ev.message_id, "model": ev.model},
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
        self._emit("session_state", {"state": state})
        if state in ("ready", "busy"):
            with contextlib.suppress(RuntimeError):
                self.ensure_tailing()
            self._start_ticks()

    def on_fatal(self, text: str, tail: str) -> None:
        data = self._report_error("fatal", text, tail=self._redactor.redact(tail))
        self._spawn(self._fire("onError", data))

    def on_restart(self, count: int, resumed: bool) -> None:
        self._subagents.clear()
        self._emit(
            "session_state",
            {
                "state": self.session.snapshot()["state"],
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
        self._emit("stall_suspected", {"turn_id": turn_id})

    def on_turn_resumed(self, record: Any, restart: int) -> None:
        # The continue prompt carries this turn's marker, so its Stop completes the same record;
        # the usage totals are not reset, so they span the crash.
        self._last_marker = record.turn_id
        self._emit("turn_resumed", {"turn_id": record.turn_id, "restart": restart})

    def on_turn_end(self, record: Any) -> None:
        # Completed turns are finished by the Stop hook handler, which also has the usage.
        if record.state == "completed" or record.state not in TERMINAL_STATES:
            return
        if self._metrics is not None:
            self._metrics.turn_finished(record)
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
        self._subagents.clear()
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
