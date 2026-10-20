"""Claude Code JSONL transcript reader: typed events, usage totals, tailer, subagent watcher.

The transcript format is internal to Claude Code and undocumented, so the parser is tolerant
(unknown line types and fields are ignored) and pinned by real captured lines in
`tests/fixtures/transcripts/`. Transcripts are the source of assistant text, tool use and usage;
the Stop hook only says that a turn ended.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .redact import Redactor

log = logging.getLogger(__name__)

#: Claude Code project directory name (CLAUDE_CODE_PROJECT_DIR_NAME) the service pins.
PROJECT_DIR_NAME = "workdir"
#: A partial line longer than this is discarded (matches the Go tailer's 16 MiB cap).
MAX_LINE_BYTES = 16 * 1024 * 1024
#: Tool result text is cut to this many characters.
MAX_RESULT_CHARS = 4000
TRUNCATED_SUFFIX = "...[truncated]"
INTERRUPT_PREFIX = "[Request interrupted by user"

_TURN_MARKER = re.compile(r"<!--\s*ccw turn:\s*(\S+?)\s*-->")
_PROBE_MARKER = re.compile(r"<!--\s*ccw probe:\s*(\S+?)\s*-->")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")


def transcript_path(
    config_dir: str | Path, session_id: str, project_dir_name: str = PROJECT_DIR_NAME
) -> Path:
    """`<config_dir>/projects/<project dir>/<session id>.jsonl`."""
    return Path(config_dir) / "projects" / project_dir_name / f"{session_id}.jsonl"


def mangle_cwd(path: str | Path) -> str:
    """Claude's default project dir name for a cwd; only a fallback, the service pins a name."""
    return _NON_ALNUM.sub("-", str(path))


# ------------------------------------------------------------------ events
@dataclass(kw_only=True)
class _Event:
    sidechain: bool = False
    agent_id: str | None = None


@dataclass
class UserPrompt(_Event):
    text: str
    turn_marker: str | None = None


@dataclass
class AssistantText(_Event):
    text: str
    message_id: str | None = None
    model: str | None = None


@dataclass
class Thinking(_Event):
    text: str
    message_id: str | None = None


@dataclass
class ToolUse(_Event):
    id: str
    name: str
    input: Any


@dataclass
class ToolResult(_Event):
    tool_use_id: str
    is_error: bool
    text: str


@dataclass
class Usage(_Event):
    message_id: str
    model: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class Interrupted(_Event):
    pass


@dataclass
class ProbeMarker(_Event):
    probe_id: str


Event = (
    UserPrompt | AssistantText | Thinking | ToolUse | ToolResult | Usage | Interrupted | ProbeMarker
)
OnEvents = Callable[[list[Event]], Awaitable[None] | None]
#: (event class, constructor kwargs) before the sidechain/agent flags are attached.
_Made = tuple[type[_Event], dict[str, Any]]


# ------------------------------------------------------------------ parsing
def _redact(redactor: Redactor | None, value: Any) -> Any:
    return redactor.redact_obj(value) if redactor else value


def _flatten(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)
        )
    return ""


def _truncate(text: str) -> str:
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + TRUNCATED_SUFFIX


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _prompt_events(text: str, redactor: Redactor | None) -> list[_Made]:
    if text.startswith(INTERRUPT_PREFIX):
        return [(Interrupted, {})]
    probe = _PROBE_MARKER.search(text)
    if probe:
        return [(ProbeMarker, {"probe_id": probe.group(1)})]
    turn = _TURN_MARKER.search(text)
    return [
        (
            UserPrompt,
            {"text": _redact(redactor, text), "turn_marker": turn.group(1) if turn else None},
        )
    ]


def parse_line(
    line: str | bytes,
    *,
    redactor: Redactor | None = None,
    sidechain: bool = False,
    agent_id: str | None = None,
) -> list[Event]:
    """Typed events for one JSONL transcript line; unknown or malformed lines yield nothing."""
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        log.debug("skipping malformed transcript line (%d bytes)", len(line))
        return []
    if not isinstance(obj, dict):
        return []
    message = obj.get("message")
    kind = obj.get("type")
    if kind not in ("user", "assistant") or not isinstance(message, dict):
        return []
    flags = {
        "sidechain": bool(sidechain or obj.get("isSidechain")),
        "agent_id": agent_id or _str(obj.get("agentId")),
    }
    content = message.get("content")
    made = (
        _user_events(content, redactor)
        if kind == "user"
        else _assistant_events(message, content, redactor)
    )
    return [cls(**kw, **flags) for cls, kw in made]


def _user_events(content: Any, redactor: Redactor | None) -> list[_Made]:
    if isinstance(content, str):
        return _prompt_events(content, redactor)
    made: list[_Made] = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            text = _truncate(_flatten(block.get("content")))
            made.append(
                (
                    ToolResult,
                    {
                        "tool_use_id": str(block.get("tool_use_id", "")),
                        "is_error": bool(block.get("is_error")),
                        "text": _redact(redactor, text),
                    },
                )
            )
        elif block.get("type") == "text" and isinstance(block.get("text"), str):
            made.extend(_prompt_events(block["text"], redactor))
    return made


def _assistant_events(message: dict, content: Any, redactor: Redactor | None) -> list[_Made]:
    mid, model = _str(message.get("id")), _str(message.get("model"))
    made: list[_Made] = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and isinstance(block.get("text"), str):
            text = _redact(redactor, block["text"])
            made.append((AssistantText, {"text": text, "message_id": mid, "model": model}))
        elif btype == "thinking" and isinstance(block.get("thinking"), str):
            text = _redact(redactor, block["thinking"])
            made.append((Thinking, {"text": text, "message_id": mid}))
        elif btype == "tool_use":
            made.append(
                (
                    ToolUse,
                    {
                        "id": str(block.get("id", "")),
                        "name": str(block.get("name", "")),
                        "input": _redact(redactor, block.get("input", {})),
                    },
                )
            )
    usage = message.get("usage")
    if made and isinstance(usage, dict) and mid:
        counts = {name: _int(usage.get(name)) for name in _USAGE_FIELDS}
        made.append((Usage, {"message_id": mid, "model": model, **counts}))
    return made


# ------------------------------------------------------------------ usage
_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


class UsageTotals:
    """Per-model token sums; claude repeats one message's usage on every content block."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._seen: set[str] = set()
        self._totals: dict[str, dict[str, int]] = {}
        self.last_usage: Usage | None = None

    def add(self, usage: Usage) -> bool:
        """Count `usage` unless its message id was already counted; returns whether it was."""
        if usage.message_id in self._seen:
            return False
        self._seen.add(usage.message_id)
        bucket = self._totals.setdefault(usage.model or "unknown", dict.fromkeys(_USAGE_FIELDS, 0))
        for name in _USAGE_FIELDS:
            bucket[name] += getattr(usage, name)
        self.last_usage = usage
        return True

    def as_dict(self) -> dict[str, dict[str, int]]:
        return {model: dict(b) for model, b in self._totals.items()}


# ------------------------------------------------------------------ tailer
class TranscriptTailer:
    """Follows one JSONL file and delivers typed events per batch of appended lines."""

    def __init__(
        self,
        path: str | Path,
        on_events: OnEvents,
        *,
        poll_interval: float = 0.2,
        redactor: Redactor | None = None,
        sidechain: bool = False,
        agent_id: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.agent_id = agent_id
        self._on_events = on_events
        self._poll_interval = poll_interval
        self._redactor = redactor
        self._sidechain = sidechain
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self._reset_position()

    def _reset_position(self) -> None:
        self._offset = 0
        self._inode: int | None = None
        self._buffer = b""
        self._discarding = False

    def switch(self, path: str | Path) -> None:
        """Follow another file from its start."""
        self.path = Path(path)
        self._reset_position()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.flush()
            except Exception:
                log.exception("transcript tailer poll failed for %s", self.path)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), self._poll_interval)

    async def flush(self) -> None:
        """Read every complete line currently in the file and deliver it before returning."""
        async with self._lock:
            events: list[Event] = []
            for raw in self._read_new_lines():
                events.extend(
                    parse_line(
                        raw,
                        redactor=self._redactor,
                        sidechain=self._sidechain,
                        agent_id=self.agent_id,
                    )
                )
            if not events:
                return
            try:
                result = self._on_events(events)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                log.exception("transcript on_events callback failed for %s", self.path)

    def _read_new_lines(self) -> list[bytes]:
        try:
            st = self.path.stat()
        except OSError:
            return []
        if self._inode is not None and (st.st_ino != self._inode or st.st_size < self._offset):
            log.info("transcript %s rotated or truncated; rereading from the start", self.path)
            self._reset_position()
        self._inode = st.st_ino
        if st.st_size <= self._offset:
            return []
        try:
            with self.path.open("rb") as f:
                f.seek(self._offset)
                data = f.read(st.st_size - self._offset)
        except OSError:
            return []
        self._offset += len(data)
        return self._split(data)

    def _split(self, data: bytes) -> list[bytes]:
        """Complete lines of `data`; the partial tail stays buffered for the next read."""
        *complete, tail = (self._buffer + data).split(b"\n")
        self._buffer = b""
        lines: list[bytes] = []
        for i, piece in enumerate(complete):
            if i == 0 and self._discarding:
                self._discarding = False  # the oversized line ends here
                continue
            if len(piece) > MAX_LINE_BYTES:
                self._warn_oversize()
            elif piece.strip():
                lines.append(piece)
        if self._discarding and not complete:
            return lines
        if len(tail) > MAX_LINE_BYTES:
            self._warn_oversize()
            self._discarding = True
        else:
            self._buffer = tail
        return lines

    def _warn_oversize(self) -> None:
        log.warning("discarding transcript line longer than 16 MiB in %s", self.path)


# ------------------------------------------------------------------ subagents
class SubagentWatcher:
    """Starts one sidechain tailer per `<transcript dir>/<session id>/subagents/agent-*.jsonl`."""

    def __init__(
        self,
        session_transcript_path: str | Path,
        on_events: OnEvents,
        poll_interval: float = 1.0,
        redactor: Redactor | None = None,
    ) -> None:
        self.path = Path(session_transcript_path)
        self._on_events = on_events
        self._poll_interval = poll_interval
        self._redactor = redactor
        self._tailers: dict[Path, TranscriptTailer] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()
        self._running = False

    @property
    def subagents_dir(self) -> Path:
        return self.path.parent / self.path.stem / "subagents"

    @property
    def agent_ids(self) -> list[str]:
        return sorted(t.agent_id or "" for t in self._tailers.values())

    def switch(self, session_transcript_path: str | Path) -> None:
        """Follow the subagents of another session transcript."""
        self._stop_tailers()
        self.path = Path(session_transcript_path)

    def _scan(self) -> list[TranscriptTailer]:
        for file in sorted(self.subagents_dir.glob("agent-*.jsonl")):
            if file in self._tailers:
                continue
            tailer = TranscriptTailer(
                file,
                self._on_events,
                poll_interval=self._poll_interval,
                redactor=self._redactor,
                sidechain=True,
                agent_id=file.stem.removeprefix("agent-"),
            )
            self._tailers[file] = tailer
            if self._running:
                self._tasks.append(asyncio.create_task(tailer.run()))
        return list(self._tailers.values())

    async def run(self) -> None:
        self._running = True
        try:
            while not self._stop.is_set():
                self._scan()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), self._poll_interval)
        finally:
            self._running = False
            self._stop_tailers()

    async def flush(self) -> None:
        """Deliver every complete line of every subagent transcript currently on disk."""
        for tailer in self._scan():
            await tailer.flush()

    def _stop_tailers(self) -> None:
        for tailer in self._tailers.values():
            tailer.stop()
        for task in self._tasks:
            task.cancel()
        self._tailers.clear()
        self._tasks.clear()

    def stop(self) -> None:
        self._stop.set()
        self._stop_tailers()
