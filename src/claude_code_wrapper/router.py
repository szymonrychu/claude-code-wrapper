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
from .hooks import ActionResult, HookDispatcher, HookOutcome, build_payload
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
from .turns import TERMINAL_STATES, Origin, TurnRecord, TurnStore
from .workdir import safe_write_files

log = logging.getLogger(__name__)

ALIVE_TOKEN = "CCW-ALIVE"
QUESTION_DENY_REASON = (
    "Question forwarded to the API client. Ask the user in plain text, "
    "end your turn now and wait for their reply in the next message."
)
DEFAULT_DENY_REASON = "Denied by a wrapper hook."
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


def _allow() -> RelayResponse:
    return {"stdout": "", "stderr": "", "exit_code": 0}


def _additional_context(event_name: str, context: str) -> str:
    return json.dumps(
        {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": context}}
    )


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
        self._closed = False
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

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        task.add_done_callback(self._log_failure)

    @staticmethod
    def _log_failure(task: asyncio.Future[Any]) -> None:
        if not task.cancelled() and task.exception() is not None:
            log.error("router background task failed", exc_info=task.exception())

    async def drain(self, timeout: float | None = None) -> None:
        """Wait for background hook fires and callback deliveries started so far."""
        pending = list(self._background)
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    async def _fire(
        self, event: str, data: Mapping[str, Any], *, tool_name: str | None = None
    ) -> HookOutcome:
        try:
            current = self.session.current_turn
            payload = build_payload(
                event,
                session_id=self.info.session_id,
                claude_session_id=self.info.claude_session_id,
                turn_id=current.turn_id if current is not None else None,
                workdir=self.info.workdir,
                data=data,
            )
            return await self._dispatcher.fire(event, payload, tool_name=tool_name)
        except Exception:  # noqa: BLE001 - user hooks never break the router
            log.exception("firing %s failed", event)
            return HookOutcome()

    def _report_error(self, kind: str, message: str, **extra: Any) -> dict[str, Any]:
        data = {"kind": kind, "message": self._redactor.redact(message), **extra}
        self._emit("error", data)
        return data

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
            try:
                data = self._report_error(
                    "hook_relay", f"{event_name}: {type(exc).__name__}: {exc}"
                )
                await self._fire("onError", data)
            except Exception:  # noqa: BLE001
                log.exception("reporting hook relay failure failed")
            return _allow()

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
        data = {"tool_use_id": tool_use_id, "tool_name": tool_name, "tool_input": tool_input}
        self._emit("tool_call", data)
        outcome = await self._fire("onAgentToolCall", data, tool_name=tool_name)
        if self._metrics is not None:
            self._metrics.tool_call(tool_name, "deny" if outcome.decision == "deny" else "allow")
        if outcome.decision == "deny":
            reason = outcome.reason or DEFAULT_DENY_REASON
            self._emit(
                "tool_denied",
                {"tool_use_id": tool_use_id, "tool_name": tool_name, "reason": reason},
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
        }
        self._emit("tool_result", data)
        self.session.record_tool_call()
        outcome = await self._fire("onToolResult", data, tool_name=tool_name)
        if outcome.additional_context:
            return {
                "stdout": _additional_context(event_name, outcome.additional_context),
                "stderr": "",
                "exit_code": 0,
            }
        return _allow()

    async def _on_stop(self, event_name: str, payload: Mapping[str, Any]) -> RelayResponse:
        await self._flush_transcripts()
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

    async def _on_subagent(self, event_name: str, payload: Mapping[str, Any]) -> RelayResponse:
        started = event_name == "SubagentStart"
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
        await self._fire("onTurnComplete", record.model_dump(mode="json", by_alias=True))
        self._deliver(self._callback_url(record), event)

    async def submit_turn(
        self,
        text: str,
        *,
        files: Mapping[str, str] | None = None,
        callback_url: str | None = None,
        origin: Origin = "client",
    ) -> TurnRecord:
        outcome = await self._fire(
            "onTurn", {"text": text, "files": list((files or {}).keys()), "origin": origin}
        )
        if outcome.decision == "deny":
            reason = outcome.reason or DEFAULT_DENY_REASON
            self._emit("turn_rejected", {"reason": reason})
            raise TurnVetoed(reason)
        if files:
            safe_write_files(
                self.info.workdir,
                files,
                max_bytes=self._cfg.limits.max_file_bytes,
                max_files=self._cfg.limits.max_files_per_turn,
            )
        record = await self.session.submit(
            text,
            origin=origin,
            callback_url=callback_url,
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
            await self._fire("onInterrupt", {"turn_id": turn_id})
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
                self._usage.add(ev)
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
        if ALIVE_TOKEN in ev.text:
            for probe in self.session.probe_answered():
                self._emit("probe", probe.model_dump(mode="json"))
        await self._fire(
            "onAgentResponse",
            {"text": ev.text, "message_id": ev.message_id, "model": ev.model},
        )

    # ------------------------------------------------------------------ SessionListener

    def on_state(self, state: str) -> None:
        self._emit("session_state", {"state": state})
        if state in ("ready", "busy"):
            with contextlib.suppress(RuntimeError):
                self.ensure_tailing()

    def on_fatal(self, text: str, tail: str) -> None:
        data = self._report_error("fatal", text, tail=self._redactor.redact(tail))
        self._spawn(self._fire("onError", data))

    def on_restart(self, count: int, resumed: bool) -> None:
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
        self._emit(
            "hook_result",
            {
                "event": result.event,
                "index": result.index,
                "type": result.type,
                "ok": result.ok,
                "decision": result.decision,
                "duration_ms": result.duration_ms,
                "error": result.error,
            },
        )

    # ------------------------------------------------------------------ lifecycle

    async def close(self, drain_timeout: float = CLOSE_DRAIN_SECONDS) -> None:
        """Stop tailing, wait up to `drain_timeout` for background fires, then cancel them."""
        if self._closed:
            return
        with contextlib.suppress(Exception):
            await self._flush_transcripts()
        self._closed = True
        if self._tailer is not None:
            self._tailer.stop()
        if self._watcher is not None:
            self._watcher.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self.drain(max(drain_timeout, 0.0))
        for task in list(self._background):
            task.cancel()
        self._emit("session_closed", {})
