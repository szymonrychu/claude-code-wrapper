"""End to end: the whole service against the scripted fake claude (D15, D20, D22).

`run_service` runs in the test loop with both ports 0. Every request goes over real HTTP to the
public port; claude's hooks travel through the venv's real `ccw-hook` relay to the real internal
port. Every user hook event runs `fixtures/hooks/record_hook.sh` (which denies the Write tool on
onAgentToolCall), and onTurnComplete also posts a signed webhook to a loopback receiver that
records turn callbacks too. The fake's turns come from `fixtures/scenarios/e2e.json`.

Every wait is bounded: a hung step fails the test with what it was waiting for.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import socket
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from claude_code_wrapper import service
from claude_code_wrapper.config import HOOK_EVENTS, Config
from claude_code_wrapper.service import ServiceHandle, run_service

FIXTURES = Path(__file__).parent / "fixtures"
SCENARIO = FIXTURES / "scenarios" / "e2e.json"
RECORD_HOOK = FIXTURES / "hooks" / "record_hook.sh"

AUTH_TOKEN = "e2e-auth-token-0123456789abcdef"
OAUTH_TOKEN = "sk-ant-oat01-e2e-fake-oauth-secret-0123456789abcdef"
CALLBACK_SECRET = "e2e-callback-secret-0123456789"
WEBHOOK_SECRET = "e2e-webhook-secret-0123456789"
DEADLINE = 15.0

# Prompts matched by the e2e scenario's turn regexes.
LIFECYCLE_PROMPT = "run the e2e lifecycle"
SLEEP_PROMPT = "sleep for a while"
FATAL_PROMPT = "trigger a fatal error"
ASK_PROMPT = "ask the user a question"
WRITE_DENIED = "record_hook: the Write tool is denied in e2e"
FATAL_PATTERN = "API Error: 401"
SUBAGENT_ID = "e2esub01"

BOOT = {
    "timeout_seconds": 10.0,
    "min_boot_seconds": 0.2,
    "idle_ready_seconds": 30.0,
    "submit_delay_seconds": 0.1,
    "dialogs": [
        {"pattern": "Bypass Permissions mode", "keys": ["\x1b[B", "\r"], "delay_seconds": 0.05}
    ],
}


def scenario_turn(match: str) -> dict[str, Any]:
    """The steps the e2e scenario runs for prompts matching `match`."""
    for turn in json.loads(SCENARIO.read_text())["turns"]:
        if turn["match"] == match:
            return turn
    raise KeyError(match)


def signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def until(probe: Callable[[], Any], what: str, timeout: float = DEADLINE) -> Any:
    """Poll `probe` until it returns something truthy; fail naming `what` at the deadline."""
    end = time.monotonic() + timeout
    while True:
        value = probe()
        if value:
            return value
        if time.monotonic() > end:
            raise AssertionError(f"timed out after {timeout:g}s waiting for {what}")
        await asyncio.sleep(0.05)


def in_order(events: list[dict], expected: list[tuple[str, str | None]]) -> list[dict]:
    """The events matching `expected` (type, tool_name or None) as an ordered subsequence."""
    found: list[dict] = []
    it = iter(events)
    for want_type, want_tool in expected:
        for ev in it:
            if ev["type"] == want_type and (
                want_tool is None or ev["data"].get("tool_name") == want_tool
            ):
                found.append(ev)
                break
        else:
            seen = [(e["type"], e["data"].get("tool_name")) for e in events]
            raise AssertionError(f"{want_type} {want_tool or ''} not in order; stream: {seen}")
    return found


# ---------------------------------------------------------------- loopback receiver


@dataclass
class Delivery:
    path: str
    headers: dict[str, str]
    body: bytes

    @property
    def json(self) -> dict[str, Any]:
        return json.loads(self.body)

    def signed_with(self, secret: str) -> bool:
        got = self.headers.get("x-ccw-signature", "")
        return hmac.compare_digest(got, signature(secret, self.body))


class Receiver:
    """Records every POST (hook webhooks on /webhook, turn callbacks on /cb)."""

    def __init__(self) -> None:
        self.deliveries: list[Delivery] = []
        self.url = ""
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._sock: socket.socket | None = None

    async def _app(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        self.deliveries.append(Delivery(scope["path"], headers, body))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.setblocking(False)
        self.url = f"http://127.0.0.1:{self._sock.getsockname()[1]}"
        config = uvicorn.Config(
            self._app, interface="asgi3", log_config=None, log_level="warning", lifespan="off"
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve(sockets=[self._sock]))
        await until(lambda: self._server is not None and self._server.started, "receiver start")

    async def close(self) -> None:
        if self._server is not None and self._task is not None:
            self._server.should_exit = True
            await asyncio.wait_for(self._task, DEADLINE)
        if self._sock is not None:
            self._sock.close()

    def on(self, path: str) -> list[Delivery]:
        return [d for d in self.deliveries if d.path == path]


# ---------------------------------------------------------------- SSE reader


class SseReader:
    """Reads one session's SSE stream in a background task, keeping raw lines and events."""

    def __init__(self, client: httpx.AsyncClient, url: str) -> None:
        self.lines: list[str] = []
        self.events: list[dict[str, Any]] = []
        self.status: int | None = None
        self.content_type = ""
        self._client = client
        self._url = url
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> SseReader:
        self._task = asyncio.create_task(self._run())
        await until(lambda: self.status is not None or self._task.done(), "SSE stream open")
        if self._task.done():
            self._task.result()
        assert self.status == 200, self.status
        assert self.content_type.startswith("text/event-stream")
        return self

    async def _run(self) -> None:
        async with self._client.stream("GET", self._url) as resp:
            self.status = resp.status_code
            self.content_type = resp.headers.get("content-type", "")
            frame: dict[str, str] = {}
            async for line in resp.aiter_lines():
                self.lines.append(line)
                if line == "":
                    if "data" in frame:
                        self.events.append(json.loads(frame["data"]))
                    frame = {}
                elif not line.startswith(":"):
                    key, _, value = line.partition(":")
                    frame[key] = value[1:] if value.startswith(" ") else value

    @property
    def done(self) -> bool:
        return self._task is not None and self._task.done()

    def of(self, type_: str, pred: Callable[[dict], bool] = lambda d: True) -> list[dict]:
        return [e for e in self.events if e["type"] == type_ and pred(e["data"])]

    async def wait(
        self, type_: str, pred: Callable[[dict], bool] = lambda d: True, timeout: float = DEADLINE
    ) -> dict[str, Any]:
        found = await until(lambda: self.of(type_, pred), f"SSE {type_}", timeout)
        return found[0]

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, httpx.HTTPError):
                await self._task


# ---------------------------------------------------------------- the service under test


@dataclass
class Running:
    task: asyncio.Task[None]
    stop: asyncio.Event
    handle: ServiceHandle

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.handle.public_port}"


@dataclass
class E2E:
    """The tmp world: config, running services, receiver, HTTP client and everything seen."""

    tmp: Path
    fake_bin: Path
    managers: list[Any]
    receiver: Receiver = field(default_factory=Receiver)
    bodies: list[str] = field(default_factory=list)
    hook_tokens: set[str] = field(default_factory=set)
    readers: list[SseReader] = field(default_factory=list)
    services: list[Running] = field(default_factory=list)
    client: httpx.AsyncClient | None = None

    @property
    def workdir_root(self) -> Path:
        return self.tmp / "workdir"

    @property
    def sessions_root(self) -> Path:
        return self.tmp / "sessions"

    @property
    def hook_log(self) -> Path:
        return self.tmp / "hooks.jsonl"

    @property
    def fake_log(self) -> Path:
        return self.tmp / "fake-claude.jsonl"

    def config(self, **sections: dict[str, Any]) -> Config:
        def record(**kw: Any) -> dict[str, Any]:
            return {
                "type": "command",
                "run": f"sh '{RECORD_HOOK}'",
                "env": {"RECORD_HOOK_LOG": str(self.hook_log)},
                **kw,
            }

        hooks: dict[str, list[dict[str, Any]]] = {event: [record()] for event in HOOK_EVENTS}
        hooks["onAgentToolCall"] = [record(blocking=True)]
        hooks["onTurnComplete"].append(
            {
                "type": "webhook",
                "url": f"{self.receiver.url}/webhook",
                "secret": WEBHOOK_SECRET,
                "retries": 0,
            }
        )
        data: dict[str, Any] = {
            "server": {
                "host": "127.0.0.1",
                "port": 0,
                "internal_port": 0,
                "auth_token": AUTH_TOKEN,
                "callback_secret": CALLBACK_SECRET,
                "log_level": "warning",
            },
            "paths": {
                "workdir_root": str(self.workdir_root),
                "sessions_root": str(self.sessions_root),
            },
            "claude": {
                "binary": str(self.fake_bin),
                "boot": BOOT,
                "env": {
                    "claude_code_oauth_token": OAUTH_TOKEN,
                    "extra": {
                        "FAKE_CLAUDE_SCENARIO": str(SCENARIO),
                        "FAKE_CLAUDE_LOG": str(self.fake_log),
                    },
                },
            },
            "mise": {"trusted_paths": [str(self.workdir_root)]},
            "hooks": hooks,
            "sessions": {"interrupt_settle_seconds": 3},
            "limits": {"shutdown_timeout_seconds": 3, "sse_heartbeat_seconds": 1},
        }
        for key, value in sections.items():
            data[key] = {**data.get(key, {}), **value}
        return Config.model_validate(data)

    # ---- service lifecycle

    async def start(self, cfg: Config | None = None) -> Running:
        ready, stop, handle = asyncio.Event(), asyncio.Event(), ServiceHandle()
        task = asyncio.create_task(
            run_service(cfg or self.config(), ready=ready, stop=stop, handle=handle)
        )
        waiter = asyncio.create_task(ready.wait())
        await asyncio.wait({task, waiter}, timeout=DEADLINE, return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        if task.done():
            task.result()
        assert ready.is_set(), "service did not become ready"
        assert handle.public_port > 0 and handle.internal_port > 0
        running = Running(task, stop, handle)
        self.services.append(running)
        if self.client is not None:
            await self.client.aclose()
        self.client = httpx.AsyncClient(
            base_url=running.base,
            headers={"Authorization": f"Bearer {AUTH_TOKEN}"},
            timeout=httpx.Timeout(DEADLINE, read=30.0),
        )
        return running

    async def stop(self, running: Running) -> None:
        for reader in self.readers:
            await reader.close()
        running.stop.set()
        await asyncio.wait_for(running.task, DEADLINE + 10)
        self.services.remove(running)

    async def close(self) -> None:
        for reader in self.readers:
            await reader.close()
        for running in list(self.services):
            with contextlib.suppress(Exception):
                await self.stop(running)
        if self.client is not None:
            await self.client.aclose()
        await self.receiver.close()

    # ---- HTTP

    async def call(
        self, method: str, path: str, status: int | tuple[int, ...] = 200, **kw: Any
    ) -> httpx.Response:
        assert self.client is not None, "no service running"
        resp = await self.client.request(method, path, **kw)
        self.bodies.append(resp.text)
        statuses = (status,) if isinstance(status, int) else status
        assert resp.status_code in statuses, f"{method} {path}: {resp.status_code} {resp.text}"
        return resp

    async def create(self, **body: Any) -> dict[str, Any]:
        snapshot = (await self.call("POST", "/v1/sessions", 201, json=body)).json()
        self.capture_tokens()
        return snapshot

    async def resume(self, session_id: str) -> dict[str, Any]:
        snapshot = (await self.call("POST", f"/v1/sessions/{session_id}/resume")).json()
        self.capture_tokens()
        return snapshot

    def capture_tokens(self) -> None:
        """The relay hook tokens of every live session (never exposed over the API)."""
        for manager in self.managers:
            for ctx in manager._active.values():
                self.hook_tokens.add(ctx.hook_token)

    async def sse(self, session_id: str) -> SseReader:
        assert self.client is not None
        reader = SseReader(self.client, f"/v1/sessions/{session_id}/events")
        self.readers.append(reader)
        return await reader.start()

    async def submit(self, session_id: str, text: str, **body: Any) -> str:
        resp = await self.call(
            "POST", f"/v1/sessions/{session_id}/turns", 202, json={"text": text, **body}
        )
        return resp.json()["turn_id"]

    async def turn(self, session_id: str, turn_id: str) -> dict[str, Any]:
        return (await self.call("GET", f"/v1/sessions/{session_id}/turns/{turn_id}")).json()

    async def finished(self, session_id: str, turn_id: str) -> dict[str, Any]:
        """Poll the turn until it reaches a terminal state."""
        end = time.monotonic() + DEADLINE
        while True:
            record = await self.turn(session_id, turn_id)
            if record["state"] != "running":
                return record
            if time.monotonic() > end:
                raise AssertionError(f"turn {turn_id} still running after {DEADLINE:g}s")
            await asyncio.sleep(0.1)

    # ---- hook log

    def hook_entries(self) -> list[dict[str, Any]]:
        if not self.hook_log.exists():
            return []
        out = []
        for line in self.hook_log.read_text().splitlines():
            with contextlib.suppress(ValueError):
                out.append(json.loads(line))
        return out

    def hooks(
        self, event: str, pred: Callable[[dict[str, Any]], bool] = lambda p: True
    ) -> list[dict[str, Any]]:
        """Payloads recorded for `event` that satisfy `pred`."""
        entries = self.hook_entries()
        return [e["payload"] for e in entries if e["event"] == event and pred(e["payload"])]

    async def hook(
        self, event: str, pred: Callable[[dict[str, Any]], bool] = lambda p: True
    ) -> dict[str, Any]:
        return (await until(lambda: self.hooks(event, pred), f"hook {event}"))[0]


@pytest.fixture
async def e2e(
    tmp_path: Path, fake_claude_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[E2E]:
    managers: list[Any] = []
    real = service.SessionManager

    class RecordingManager(real):  # type: ignore[valid-type, misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            managers.append(self)

    monkeypatch.setattr(service, "SessionManager", RecordingManager)
    world = E2E(tmp_path, fake_claude_bin, managers)
    await world.receiver.start()
    try:
        yield world
    finally:
        await world.close()


# ---------------------------------------------------------------- scenario 1: the lifecycle


@dataclass
class Lifecycle:
    created: dict[str, Any]
    session_id: str
    turn_id: str
    record: dict[str, Any]
    events: list[dict[str, Any]]
    transcript: httpx.Response
    destroyed: dict[str, Any]
    after: dict[str, Any]


async def run_lifecycle(w: E2E) -> Lifecycle:
    """Create, stream, one scenario turn to completion, transcript, then destroy."""
    await w.start()
    created = await w.create(name="e2e")
    sid = created["session_id"]
    sse = await w.sse(sid)
    tid = await w.submit(sid, LIFECYCLE_PROMPT)
    record = await w.finished(sid, tid)
    await sse.wait("turn_completed", lambda d: d["record"]["turn_id"] == tid)
    await w.hook("onTurnComplete", lambda p: p["data"]["turn_id"] == tid)
    await until(lambda: w.receiver.on("/webhook"), "onTurnComplete webhook")
    transcript = await w.call("GET", f"/v1/sessions/{sid}/transcript")
    await w.call("GET", f"/v1/sessions/{sid}/history", params={"after_seq": 0, "limit": 1000})
    await w.call("GET", f"/v1/sessions/{sid}")
    await w.call("GET", f"/v1/sessions/{sid}/turns")
    await w.call("GET", "/v1/sessions")
    await w.call("GET", "/v1/config")
    destroyed = (await w.call("DELETE", f"/v1/sessions/{sid}")).json()
    await sse.wait("session_closed")
    await until(lambda: sse.done, "SSE stream end after DELETE")
    after = (await w.call("GET", f"/v1/sessions/{sid}")).json()
    return Lifecycle(created, sid, tid, record, list(sse.events), transcript, destroyed, after)


async def test_full_lifecycle_events_and_hooks(e2e: E2E) -> None:
    run = await run_lifecycle(e2e)
    sid, tid = run.session_id, run.turn_id
    assert run.created["state"] == "ready"
    assert run.record["state"] == "completed"
    assert run.record["final_text"] == "All done."

    turn_events = [
        e
        for e in run.events
        if e["session_id"] == sid and e["type"] not in ("hook_result", "session_state")
    ]
    in_order(
        turn_events,
        [
            ("turn_started", None),
            ("tool_call", "Bash"),
            ("tool_result", "Bash"),
            ("tool_call", "Write"),
            ("tool_denied", "Write"),
            ("subagent_started", None),
            ("subagent_stopped", None),
            ("turn_completed", None),
        ],
    )
    completed_at = next(i for i, e in enumerate(run.events) if e["type"] == "turn_completed")
    main_texts = [
        e["data"]["text"]
        for e in run.events[:completed_at]
        if e["type"] == "assistant_text" and e["data"]["agent_id"] is None
    ]
    assert main_texts == ["Starting the work.", "All done."]
    assert any(
        e["type"] == "assistant_text" and e["data"]["agent_id"] == SUBAGENT_ID for e in run.events
    )
    assert any(e["type"] == "thinking" for e in run.events[:completed_at])
    assert next(e for e in run.events if e["type"] == "turn_started")["data"]["turn_id"] == tid

    def of_turn(p: dict[str, Any]) -> bool:
        return p["session_id"] == sid and p["turn_id"] == tid

    created = e2e.hooks("onCreate", lambda p: p["session_id"] == sid)
    assert len(created) == 1 and created[0]["data"]["state"] == "ready"
    turn = e2e.hooks("onTurn", lambda p: p["session_id"] == sid)
    assert len(turn) == 1 and turn[0]["data"]["text"].startswith(LIFECYCLE_PROMPT)
    tools = [p["data"]["tool_name"] for p in e2e.hooks("onAgentToolCall") if of_turn(p)]
    assert tools == ["Bash", "Write"]
    results = {p["data"]["tool_name"]: p["data"] for p in e2e.hooks("onToolResult") if of_turn(p)}
    assert results["Bash"]["is_error"] is False and results["Bash"]["response"] == "hi"
    assert [p["data"]["agent_id"] for p in e2e.hooks("onSubagentStart") if of_turn(p)] == [
        SUBAGENT_ID
    ]
    assert [p["data"]["agent_id"] for p in e2e.hooks("onSubagentStop") if of_turn(p)] == [
        SUBAGENT_ID
    ]
    responses = [p["data"]["text"] for p in e2e.hooks("onAgentResponse") if of_turn(p)]
    assert responses == ["Starting the work.", "All done."]
    complete = [p for p in e2e.hooks("onTurnComplete") if of_turn(p)]
    assert len(complete) == 1 and complete[0]["data"]["state"] == "completed"

    webhook = e2e.receiver.on("/webhook")[0]
    assert webhook.signed_with(WEBHOOK_SECRET)
    assert webhook.json["event"] == "onTurnComplete" and webhook.json["turn_id"] == tid

    assert run.transcript.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in run.transcript.text.splitlines()]
    assert lines and all(isinstance(line, dict) for line in lines)
    assert any(
        line.get("type") == "user"
        and isinstance(line["message"]["content"], str)
        and line["message"]["content"].startswith(LIFECYCLE_PROMPT)
        for line in lines
    )

    assert run.destroyed["state"] == "stopped" and run.after["state"] == "stopped"
    destroyed = await e2e.hook("onDestroy", lambda p: p["session_id"] == sid)
    assert destroyed["data"]["state"] == "stopped"


async def test_tool_gate_denies_write(e2e: E2E) -> None:
    run = await run_lifecycle(e2e)
    tid = run.turn_id
    denied = [e for e in run.events if e["type"] == "tool_denied"]
    assert [e["data"]["tool_name"] for e in denied] == ["Write"]
    assert denied[0]["data"]["reason"] == WRITE_DENIED

    results = {e["data"]["tool_name"]: e["data"] for e in run.events if e["type"] == "tool_result"}
    assert results["Bash"]["is_error"] is False
    assert results["Write"]["is_error"] is True and results["Write"]["response"] == WRITE_DENIED

    gate = [p for p in e2e.hooks("onAgentToolCall") if p["turn_id"] == tid]
    assert {p["data"]["tool_name"] for p in gate} == {"Bash", "Write"}
    write = next(p for p in gate if p["data"]["tool_name"] == "Write")
    assert write["data"]["tool_input"] == scenario_turn(LIFECYCLE_PROMPT)["steps"][3]["input"]

    # claude saw the deny: the transcript's tool_result for Write is an error with the reason.
    tool_results = [
        block
        for line in map(json.loads, run.transcript.text.splitlines())
        if line.get("type") == "user" and isinstance(line["message"]["content"], list)
        for block in line["message"]["content"]
        if block.get("type") == "tool_result"
    ]
    assert [(b["is_error"], b["content"]) for b in tool_results] == [
        (False, "hi"),
        (True, WRITE_DENIED),
    ]
    assert run.record["state"] == "completed"


async def test_webhook_signed(e2e: E2E) -> None:
    run = await run_lifecycle(e2e)
    hooks = e2e.receiver.on("/webhook")
    assert len(hooks) == 1
    delivery = hooks[0]
    assert delivery.headers["x-ccw-event"] == "onTurnComplete"
    assert delivery.headers["x-ccw-delivery"]
    assert delivery.headers["x-ccw-signature"] == signature(WEBHOOK_SECRET, delivery.body)
    assert delivery.signed_with(WEBHOOK_SECRET)
    assert not Delivery(delivery.path, delivery.headers, delivery.body + b" ").signed_with(
        WEBHOOK_SECRET
    )
    body = delivery.json
    assert body["event"] == "onTurnComplete"
    assert body["session_id"] == run.session_id and body["turn_id"] == run.turn_id
    assert body["data"]["state"] == "completed" and body["data"]["turn_id"] == run.turn_id


async def test_no_secret_leaks(e2e: E2E) -> None:
    run = await run_lifecycle(e2e)
    assert e2e.hook_tokens, "no live hook token was captured"
    config = json.loads(next(b for b in e2e.bodies if '"auth_token"' in b))
    assert config["claude"]["env"]["claude_code_oauth_token"] == "***"

    session_json = (e2e.sessions_root / run.session_id / "session.json").read_text()
    seen = {
        "response bodies": "\n".join(e2e.bodies),
        "SSE frames": "\n".join(line for r in e2e.readers for line in r.lines),
        "hook payload log": e2e.hook_log.read_text(),
        "session.json": session_json,
        "webhook and callback bodies": "\n".join(d.body.decode() for d in e2e.receiver.deliveries),
    }
    assert all(seen.values()), [k for k, v in seen.items() if not v]
    for secret in (OAUTH_TOKEN, *e2e.hook_tokens):
        for where, text in seen.items():
            assert secret not in text, f"a secret leaked into {where}"


# ---------------------------------------------------------------- scenarios 2-4


async def _interrupted_turn(w: E2E, sid: str, sse: SseReader, **body: Any) -> str:
    tid = await w.submit(sid, SLEEP_PROMPT, **body)
    await sse.wait("assistant_text", lambda d: d["text"] == "Sleeping now.")
    resp = (await w.call("POST", f"/v1/sessions/{sid}/interrupt")).json()
    assert resp == {"interrupted": True, "turn_id": tid}
    record = await w.finished(sid, tid)
    assert record["state"] == "interrupted"
    return tid


async def _fatal_turn(w: E2E, sid: str, **body: Any) -> str:
    tid = await w.submit(sid, FATAL_PROMPT, **body)
    record = await w.finished(sid, tid)
    assert record["state"] == "failed"
    assert record["error"] == FATAL_PATTERN
    return tid


async def _next_turn_completes(w: E2E, sid: str, text: str) -> None:
    tid = await w.submit(sid, text)
    record = await w.finished(sid, tid)
    assert record["state"] == "completed"
    assert record["final_text"].startswith(f"echo: {text}")


async def test_interrupt_flow(e2e: E2E) -> None:
    await e2e.start()
    sid = (await e2e.create())["session_id"]
    sse = await e2e.sse(sid)
    tid = await _interrupted_turn(e2e, sid, sse)
    assert (await sse.wait("interrupted"))["data"]["turn_id"] == tid
    ended = await sse.wait("turn_failed", lambda d: d["record"]["turn_id"] == tid)
    assert ended["data"]["record"]["state"] == "interrupted"
    await e2e.hook("onInterrupt", lambda p: p["data"]["turn_id"] == tid)
    done = await e2e.hook("onTurnComplete", lambda p: p["data"]["turn_id"] == tid)
    assert done["data"]["state"] == "interrupted"
    await _next_turn_completes(e2e, sid, "hello after the interrupt")


async def test_fatal_error_flow(e2e: E2E) -> None:
    await e2e.start()
    sid = (await e2e.create())["session_id"]
    sse = await e2e.sse(sid)
    tid = await _fatal_turn(e2e, sid)
    error = await sse.wait("error", lambda d: d.get("kind") == "fatal")
    assert error["data"]["message"] == FATAL_PATTERN
    ended = await sse.wait("turn_failed", lambda d: d["record"]["turn_id"] == tid)
    assert ended["data"]["record"]["state"] == "failed"
    on_error = await e2e.hook("onError", lambda p: p["data"].get("kind") == "fatal")
    assert on_error["session_id"] == sid and on_error["data"]["message"] == FATAL_PATTERN
    done = await e2e.hook("onTurnComplete", lambda p: p["data"]["turn_id"] == tid)
    assert done["data"]["state"] == "failed" and done["data"]["error"] == FATAL_PATTERN
    await _next_turn_completes(e2e, sid, "hello after the fatal error")


async def test_on_turn_complete_for_terminal_states(e2e: E2E) -> None:
    await e2e.start(e2e.config(limits={"turn_max_seconds": 4}))
    sid = (await e2e.create())["session_id"]
    sse = await e2e.sse(sid)
    callback = {"callbackUrl": f"{e2e.receiver.url}/cb"}
    interrupted = await _interrupted_turn(e2e, sid, sse, **callback)
    failed = await _fatal_turn(e2e, sid, **callback)
    timed_out = await e2e.submit(sid, SLEEP_PROMPT, **callback)
    record = await e2e.finished(sid, timed_out)
    assert record["state"] == "timeout"
    expected = {interrupted: "interrupted", failed: "failed", timed_out: "timeout"}

    for tid, state in expected.items():
        done = await e2e.hook("onTurnComplete", lambda p, t=tid: p["data"]["turn_id"] == t)
        assert done["session_id"] == sid and done["data"]["state"] == state
        webhook = await until(
            lambda t=tid: [
                d for d in e2e.receiver.on("/webhook") if d.json["data"]["turn_id"] == t
            ],
            f"onTurnComplete webhook for {tid}",
        )
        assert webhook[0].signed_with(WEBHOOK_SECRET)
        assert webhook[0].json["data"]["state"] == state
        cb = await until(
            lambda t=tid: [
                d for d in e2e.receiver.on("/cb") if d.json["data"]["record"]["turn_id"] == t
            ],
            f"turn callback for {tid}",
        )
        assert cb[0].signed_with(CALLBACK_SECRET)
        assert cb[0].json["type"] == "turn_failed"
        assert cb[0].json["data"]["record"]["state"] == state


async def test_ask_user_question_forwarded(e2e: E2E) -> None:
    await e2e.start()
    sid = (await e2e.create())["session_id"]
    sse = await e2e.sse(sid)
    tid = await e2e.submit(sid, ASK_PROMPT)
    record = await e2e.finished(sid, tid)
    assert record["state"] == "completed"
    asked = scenario_turn(ASK_PROMPT)["steps"][0]["input"]
    question = await sse.wait("question")
    assert question["data"]["questions"] == asked
    assert question["data"]["tool_use_id"].startswith("toolu_")
    # forwarded, not gated: the question never reaches onAgentToolCall
    assert not sse.of("tool_call", lambda d: d["tool_name"] == "AskUserQuestion")
    assert not [p for p in e2e.hooks("onAgentToolCall") if p["turn_id"] == tid]
    # claude got the deny-with-guidance and ended its turn in plain text
    result = await sse.wait("tool_result", lambda d: d["tool_name"] == "AskUserQuestion")
    assert result["data"]["is_error"] is True
    assert record["final_text"] == "Which color should I use: red or blue?"


async def test_question_posted_to_callback(e2e: E2E) -> None:
    await e2e.start()
    sid = (await e2e.create())["session_id"]
    sse = await e2e.sse(sid)
    url = f"{e2e.receiver.url}/cb"
    tid = await e2e.submit(sid, ASK_PROMPT, callbackUrl=url)
    assert (await e2e.turn(sid, tid))["callbackUrl"] == url
    await e2e.finished(sid, tid)
    question = await sse.wait("question")
    posted = await until(
        lambda: [d for d in e2e.receiver.on("/cb") if d.json["type"] == "question"],
        "question callback",
    )
    assert len(posted) == 1
    delivery = posted[0]
    assert delivery.signed_with(CALLBACK_SECRET)
    assert delivery.json == question
    assert set(delivery.json) == {"seq", "ts", "session_id", "type", "data"}
    assert delivery.json["data"]["questions"] == scenario_turn(ASK_PROMPT)["steps"][0]["input"]
    completed = await until(
        lambda: [d for d in e2e.receiver.on("/cb") if d.json["type"] == "turn_completed"],
        "turn completion callback",
    )
    assert completed[0].signed_with(CALLBACK_SECRET)
    assert completed[0].json["data"]["record"]["turn_id"] == tid
