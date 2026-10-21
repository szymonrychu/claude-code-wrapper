"""User hook dispatcher: command and signed-webhook actions that may veto."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import signal
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from claude_code_wrapper.config import (
    HOOK_EVENTS,
    VETO_EVENTS,
    CommandAction,
    HooksConfig,
    WebhookAction,
)
from claude_code_wrapper.redact import Redactor
from claude_code_wrapper.timeutil import now_iso

log = logging.getLogger(__name__)

STDERR_CAP = 2000
RETRY_BASE_SECONDS = 0.5
_KILL_GRACE_SECONDS = 2.0
OUTPUT_CAP_BYTES = 256 * 1024
ANNOTATIONS_CAP_BYTES = 16 * 1024
PARSED_DECISIONS = ("allow", "deny", "block", "defer")
# Decisions each event honours; anything else is treated as allow. The one place
# that knows deny only counts on VETO_EVENTS and block/defer only on onStop.
EVENT_DECISIONS: dict[str, tuple[str, ...]] = {
    event: ("allow", "deny") if event in VETO_EVENTS else ("allow",) for event in HOOK_EVENTS
}
EVENT_DECISIONS["onStop"] = ("allow", "block", "defer")
#: Across blocking actions the strictest honoured decision wins.
DECISION_RANK = {"allow": 0, "defer": 1, "block": 2, "deny": 2}


def build_payload(
    event: str,
    *,
    session_id: str,
    claude_session_id: str | None,
    turn_id: str | None,
    workdir: str,
    data: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "event": event,
        "session_id": session_id,
        "claude_session_id": claude_session_id,
        "turn_id": turn_id,
        "ts": now_iso(),
        "workdir": workdir,
        "data": dict(data),
    }


@dataclass
class ActionResult:
    event: str
    index: int
    type: str
    ok: bool
    decision: str = "allow"
    duration_ms: int = 0
    error: str | None = None
    reason: str | None = field(default=None, repr=False)
    additional_context: str | None = field(default=None, repr=False)
    annotations: dict[str, Any] | None = field(default=None, repr=False)
    output: Any | None = field(default=None, repr=False)
    invalid_decision: str | None = None


@dataclass
class HookOutcome:
    decision: str = "allow"
    reason: str | None = None
    additional_context: str = ""
    annotations: dict[str, Any] = field(default_factory=dict)
    outputs: list[dict[str, Any]] = field(default_factory=list)
    results: list[ActionResult] = field(default_factory=list)


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _parse_decision(text: str) -> dict[str, Any]:
    """Decision fields from a JSON object body; anything else yields nothing."""
    try:
        obj = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(obj, dict):
        return {}
    out: dict[str, Any] = {}
    if obj.get("decision") in PARSED_DECISIONS:
        out["decision"] = obj["decision"]
    for key in ("reason", "additional_context"):
        if isinstance(obj.get(key), str) and obj[key]:
            out[key] = obj[key]
    if isinstance(obj.get("annotations"), dict):
        out["annotations"] = obj["annotations"]
    if obj.get("output") is not None:
        out["output"] = obj["output"]
    return out


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode())


async def _deliver(
    client: httpx.AsyncClient,
    url: str,
    body: bytes,
    headers: dict[str, str],
    retries: int,
) -> httpx.Response:
    """POST with retries on connection errors and non-2xx; raises the last failure."""
    delay = RETRY_BASE_SECONDS
    for attempt in range(retries + 1):
        try:
            resp = await client.post(url, content=body, headers=headers)
            if resp.is_success:
                return resp
            failure: Exception = RuntimeError(f"HTTP {resp.status_code}")
        except httpx.HTTPError as e:
            failure = RuntimeError(f"{type(e).__name__}: {e}")
        if attempt == retries:
            raise failure
        await asyncio.sleep(delay)
        delay *= 2
    raise AssertionError("unreachable")  # pragma: no cover


def _headers(
    event: str | None, secret: str | None, body: bytes, extra: Mapping[str, str]
) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if event:
        headers["X-CCW-Event"] = event
    headers["X-CCW-Delivery"] = str(uuid.uuid4())
    headers.update(extra)
    if secret:
        headers["X-CCW-Signature"] = _sign(secret, body)
    return headers


async def post_json(
    url: str,
    body: Any,
    *,
    secret: str | None = None,
    retries: int = 3,
    timeout: float = 10.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    """Signed JSON delivery (also used for per-turn callback_url). Never raises."""
    raw = json.dumps(body).encode()
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
            await _deliver(client, url, raw, _headers(None, secret, raw, {}), retries)
        return True
    except Exception as e:  # noqa: BLE001 - delivery is best effort
        log.warning("post_json to %s failed: %s", url, e)
        return False


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), _KILL_GRACE_SECONDS)


def gate_fail_mode(hooks: HooksConfig, tool_name: str | None = None) -> Literal["allow", "deny"]:
    """`deny` when a blocking onAgentToolCall action with on_error deny matches `tool_name`.

    `tool_name=None` considers every action regardless of its matcher.
    """
    for a in hooks.onAgentToolCall:
        if not (a.blocking and a.on_error == "deny"):
            continue
        if tool_name is None or HookDispatcher._matches(a.matcher, tool_name):
            return "deny"
    return "allow"


class HookDispatcher:
    def __init__(
        self,
        hooks_cfg: HooksConfig,
        redactor: Redactor,
        *,
        base_env: Mapping[str, str],
        default_cwd: str | Path,
        config_dir: str | Path | None = None,
        state_dir: str | Path | None = None,
        api_url: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
        on_result: Callable[[ActionResult], None] | None = None,
    ) -> None:
        self._cfg = hooks_cfg
        self._redactor = redactor
        self._base_env = dict(base_env)
        self._default_cwd = str(default_cwd)
        self._config_dir = str(config_dir) if config_dir is not None else ""
        self._state_dir = str(state_dir) if state_dir is not None else ""
        self._api_url = api_url
        self._transport = transport
        self._on_result = on_result
        self._pending: set[asyncio.Task[Any]] = set()

    async def fire(
        self,
        event: str,
        payload: dict[str, Any],
        *,
        tool_name: str | None = None,
        cwd: str | Path | None = None,
    ) -> HookOutcome:
        outcome = HookOutcome()
        actions = [
            (i, a)
            for i, a in enumerate(self._cfg.for_event(event))
            if self._matches(a.matcher, tool_name)
        ]
        if not actions:
            return outcome
        safe = self._redactor.redact_obj(payload)
        run_cwd = str(cwd) if cwd is not None else self._default_cwd
        blocking: list[asyncio.Task[ActionResult]] = []
        for index, action in actions:
            coro = self._run(event, index, action, safe, run_cwd)
            if action.blocking:
                blocking.append(asyncio.ensure_future(coro))
            else:
                task = asyncio.ensure_future(coro)
                self._pending.add(task)
                task.add_done_callback(self._pending.discard)
        results = await asyncio.gather(*blocking) if blocking else []
        contexts: list[str] = []
        for res in results:
            outcome.results.append(res)
            if res.additional_context:
                contexts.append(res.additional_context)
            if DECISION_RANK[res.decision] > DECISION_RANK[outcome.decision]:
                outcome.decision = res.decision
                outcome.reason = res.reason
            if res.annotations:
                outcome.annotations.update(res.annotations)
            outcome.outputs.append(
                {"index": res.index, "ok": res.ok, "output": res.output, "error": res.error}
            )
        outcome.additional_context = "\n\n".join(contexts)
        return outcome

    async def post_json(
        self, url: str, body: Any, *, secret: str | None = None, retries: int = 3
    ) -> bool:
        return await post_json(
            url,
            self._redactor.redact_obj(body),
            secret=secret,
            retries=retries,
            transport=self._transport,
        )

    async def drain(self, timeout: float) -> None:
        pending = list(self._pending)
        if not pending:
            return
        _, still = await asyncio.wait(pending, timeout=timeout)
        for task in still:
            task.cancel()
        if still:
            await asyncio.gather(*still, return_exceptions=True)

    @staticmethod
    def _matches(matcher: str | None, tool_name: str | None) -> bool:
        if matcher is None:
            return True
        return tool_name is not None and re.fullmatch(matcher, tool_name) is not None

    async def _run(
        self,
        event: str,
        index: int,
        action: CommandAction | WebhookAction,
        payload: dict[str, Any],
        cwd: str,
    ) -> ActionResult:
        started = time.monotonic()
        res = ActionResult(event=event, index=index, type=action.type, ok=True)
        try:
            if isinstance(action, CommandAction):
                fields = await self._command(action, event, payload, cwd)
            else:
                try:
                    fields = await asyncio.wait_for(
                        self._webhook(action, event, payload), action.timeout_seconds
                    )
                except TimeoutError:
                    raise RuntimeError(f"timeout after {action.timeout_seconds}s") from None
            res.decision = fields.get("decision", "allow")
            res.reason = fields.get("reason")
            res.additional_context = fields.get("additional_context")
            if action.blocking:
                self._take_payload_fields(res, fields)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - nothing raises to the caller
            res.ok = False
            res.error = self._redactor.redact(str(e) or type(e).__name__)
            res.decision = action.on_error if action.blocking else "allow"
            if res.decision == "deny":
                res.reason = f"hook {event}[{index}] failed: {res.error}"
            if not action.blocking:
                log.warning("hook %s[%d] failed: %s", event, index, res.error)
        if res.decision not in EVENT_DECISIONS.get(event, ("allow",)):
            res.invalid_decision = res.decision
            res.decision = "allow"
            res.reason = None
        res.duration_ms = int((time.monotonic() - started) * 1000)
        if self._on_result is not None:
            try:
                self._on_result(res)
            except Exception:  # noqa: BLE001
                log.exception("on_result callback failed")
        return res

    def _take_payload_fields(self, res: ActionResult, fields: dict[str, Any]) -> None:
        """Redact then cap `output` and `annotations` from a blocking action's result."""
        if "annotations" in fields:
            annotations = self._redactor.redact_obj(fields["annotations"])
            size = _json_size(annotations)
            if size > ANNOTATIONS_CAP_BYTES:
                res.error = f"annotations dropped: {size} bytes exceeds {ANNOTATIONS_CAP_BYTES}"
            else:
                res.annotations = annotations
        if "output" in fields:
            output = self._redactor.redact_obj(fields["output"])
            size = _json_size(output)
            res.output = {"truncated": True, "bytes": size} if size > OUTPUT_CAP_BYTES else output

    async def _command(
        self, action: CommandAction, event: str, payload: dict[str, Any], cwd: str
    ) -> dict[str, Any]:
        env = {
            **self._base_env,
            **action.env,
            "CCW_EVENT": event,
            "CCW_SESSION_ID": str(payload.get("session_id") or ""),
            "CCW_TURN_ID": str(payload.get("turn_id") or ""),
            "CCW_WORKDIR": str(payload.get("workdir") or ""),
            "CCW_CLAUDE_CONFIG_DIR": self._config_dir,
            "CCW_STATE_DIR": self._state_dir,
            "CCW_API_URL": self._api_url,
        }
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            action.run,
            cwd=cwd,
            env=env,
            start_new_session=True,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(json.dumps(payload).encode()), action.timeout_seconds
            )
        except TimeoutError:
            await _kill_group(proc)
            raise RuntimeError(f"timeout after {action.timeout_seconds}s") from None
        except BaseException:
            await _kill_group(proc)
            raise
        code = proc.returncode
        if code == 0:
            return _parse_decision(out.decode(errors="replace"))
        stderr = err.decode(errors="replace").strip()
        if code == 2:
            decision = "block" if event == "onStop" else "deny"
            return {"decision": decision, "reason": stderr[:STDERR_CAP]}
        raise RuntimeError(f"exit {code}: {stderr[:200]}" if stderr else f"exit {code}")

    async def _webhook(
        self, action: WebhookAction, event: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        raw = json.dumps(payload).encode()
        headers = _headers(event, action.secret or None, raw, action.headers)
        async with httpx.AsyncClient(
            transport=self._transport, timeout=action.timeout_seconds
        ) as client:
            resp = await _deliver(client, action.url, raw, headers, action.retries)
        return _parse_decision(resp.text)
