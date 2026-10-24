"""Terminal routes (src/claude_code_wrapper/api/terminals.py): create, list and delete over HTTP
and the attach WebSocket (design D2, D3).

Every test drives a real `TerminalRegistry` running `/bin/sh` under a PTY in a `tmp_path`
workdir, through Starlette's `TestClient` used as a context manager so HTTP requests, WebSocket
sessions and the shells share one event loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from claude_code_wrapper.api import terminals as terminals_api
from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.config import Config, LimitsConfig
from claude_code_wrapper.manager import SessionNotFound
from claude_code_wrapper.redact import Redactor
from claude_code_wrapper.terminals import SessionNotActive, ShellTarget, TerminalRegistry

TOKEN = "tok-terminals-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SID = "ses_aaaaaaaaaaaa"
IDLE = "ses_bbbbbbbbbbbb"  # known, not active
UNKNOWN = "ses_000000000000"
MISSING_TERMINAL = "term_000000000000"
MAX_FRAME = 1024
LIMITS = {"terminals_max": 2, "terminal_max_frame_bytes": MAX_FRAME}
TERMINAL_KEYS = {
    "terminal_id",
    "session_id",
    "created_at",
    "alive",
    "exit_code",
    "attached",
    "rows",
    "cols",
}


class Host:
    """Known sessions (SID active, IDLE stopped) and their workdirs."""

    def __init__(self, tmp_path: Path) -> None:
        self.workdirs = {SID: tmp_path / "wd-active", IDLE: tmp_path / "wd-idle"}
        for workdir in self.workdirs.values():
            workdir.mkdir()
        self.active = {SID}

    def resolve(self, session_id: str) -> ShellTarget:
        if session_id not in self.workdirs:
            raise SessionNotFound(session_id)
        if session_id not in self.active:
            raise SessionNotActive(session_id, "stopped")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PS1": "$ "}
        return ShellTarget(workdir=str(self.workdirs[session_id]), env=env)


class StubManager:
    started = True

    def __init__(self, host: Host, registry: TerminalRegistry) -> None:
        self.redactor = Redactor()
        self.terminals = registry
        self._host = host

    def get(self, session_id: str) -> dict[str, Any]:
        if session_id not in self._host.workdirs:
            raise SessionNotFound(session_id)
        state = "running" if session_id in self._host.active else "stopped"
        return {"session_id": session_id, "state": state}


class Api:
    def __init__(self, client: TestClient, registry: TerminalRegistry, host: Host) -> None:
        self.client = client
        self.registry = registry
        self.host = host

    def create(self, session_id: str = SID, **body: Any) -> Any:
        return self.client.post(f"/v1/sessions/{session_id}/terminals", json=body, headers=AUTH)

    def new(self, **body: Any) -> str:
        r = self.create(**body)
        assert r.status_code == 201, r.text
        return r.json()["terminal_id"]

    def list(self, session_id: str = SID) -> Any:
        return self.client.get(f"/v1/sessions/{session_id}/terminals", headers=AUTH)

    def info(self, terminal_id: str) -> dict[str, Any]:
        (term,) = [t for t in self.list().json()["terminals"] if t["terminal_id"] == terminal_id]
        return term

    def delete(self, terminal_id: str, session_id: str = SID) -> Any:
        return self.client.delete(
            f"/v1/sessions/{session_id}/terminals/{terminal_id}", headers=AUTH
        )

    def attach(self, terminal_id: str, session_id: str = SID) -> Any:
        return self.client.websocket_connect(
            f"/v1/sessions/{session_id}/terminals/{terminal_id}/attach", headers=AUTH
        )

    def ring(self, terminal_id: str) -> bytes:
        return self.registry.ring(SID, terminal_id).raw()

    def denied(self, terminal_id: str, session_id: str = SID) -> WebSocketDenialResponse:
        with pytest.raises(WebSocketDenialResponse) as exc, self.attach(terminal_id, session_id):
            pass
        return exc.value


@pytest.fixture
def api(tmp_path: Path) -> Iterator[Api]:
    host = Host(tmp_path)
    registry = TerminalRegistry(LimitsConfig(**LIMITS), host.resolve, argv=("/bin/sh",))
    cfg = Config.model_validate({"server": {"auth_token": TOKEN}, "limits": LIMITS})
    app = create_app(StubManager(host, registry), cfg)
    with TestClient(app) as client:
        yield Api(client, registry, host)
        client.portal.call(registry.shutdown, 1.0)


# ------------------------------------------------------------------ socket helpers


def recv(ws: Any, timeout: float = 10.0) -> dict[str, Any]:
    """The next message the server sent, failing after `timeout` seconds instead of hanging."""

    async def get() -> dict[str, Any]:
        with anyio.fail_after(timeout):
            return await ws._send_rx.receive()

    return ws.portal.call(get)


def read_until(ws: Any, needle: bytes, timeout: float = 10.0) -> bytes:
    """Binary output until `needle` shows up; any other message fails the test."""
    buf = b""
    end = time.monotonic() + timeout
    while needle not in buf:
        msg = recv(ws, max(end - time.monotonic(), 0.01))
        assert msg["type"] == "websocket.send" and msg.get("bytes") is not None, (msg, buf)
        buf += msg["bytes"]
    return buf


def next_control(ws: Any, timeout: float = 10.0) -> dict[str, Any]:
    """The next text (control) frame, skipping binary output."""
    end = time.monotonic() + timeout
    while True:
        msg = recv(ws, max(end - time.monotonic(), 0.01))
        assert msg["type"] == "websocket.send", msg
        if msg.get("text") is not None:
            return json.loads(msg["text"])


def close_code(ws: Any, timeout: float = 10.0) -> int:
    """The close code the server sends next, skipping output frames."""
    end = time.monotonic() + timeout
    while True:
        msg = recv(ws, max(end - time.monotonic(), 0.01))
        if msg["type"] == "websocket.close":
            return msg["code"]
        assert msg["type"] == "websocket.send", msg
        assert msg.get("text") is None, f"unexpected control {msg['text']}"


def type_line(ws: Any, line: str) -> None:
    ws.send_bytes(line.encode() + b"\n")


def wait_for(pred: Any, timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < end, "condition not reached"
        time.sleep(0.02)


# ------------------------------------------------------------------ HTTP


def test_http_lifecycle(api: Api) -> None:
    r = api.create(rows=30, cols=100)
    assert r.status_code == 201
    first = r.json()
    assert set(first) == TERMINAL_KEYS
    assert terminals_api.TERMINAL_ID_RE.match(first["terminal_id"])
    assert first["session_id"] == SID
    assert first["alive"] is True
    assert first["exit_code"] is None
    assert first["attached"] == 0
    assert (first["rows"], first["cols"]) == (30, 100)
    assert first["created_at"].endswith("Z")

    second = api.create().json()  # no body fields: the defaults
    assert (second["rows"], second["cols"]) == (24, 80)

    r = api.client.post(f"/v1/sessions/{SID}/terminals", headers=AUTH)  # no body at all
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "terminal_limit"

    r = api.create(rows=10, cols=10, shell="/bin/bash")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"

    r = api.list()
    assert r.status_code == 200
    listed = r.json()["terminals"]
    assert {t["terminal_id"] for t in listed} == {first["terminal_id"], second["terminal_id"]}
    assert all(set(t) == TERMINAL_KEYS for t in listed)

    r = api.delete(first["terminal_id"])
    assert r.status_code == 204
    assert r.content == b""
    assert [t["terminal_id"] for t in api.list().json()["terminals"]] == [second["terminal_id"]]

    for terminal_id in (first["terminal_id"], MISSING_TERMINAL):
        r = api.delete(terminal_id)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "terminal_not_found"

    assert api.create().status_code == 201  # the slot is free again
    assert api.list(IDLE).json() == {"terminals": []}


def test_create_invalid_size(api: Api) -> None:
    for body in ({"rows": 0}, {"cols": 0}, {"rows": 1001}, {"cols": 1001}, {"rows": -5}):
        r = api.create(**body)
        assert r.status_code == 422, body
        assert r.json()["error"]["code"] == "invalid_size", body
    assert api.list().json() == {"terminals": []}
    edge = api.create(rows=1000, cols=1)
    assert edge.status_code == 201
    assert (edge.json()["rows"], edge.json()["cols"]) == (1000, 1)


def test_session_not_active_vs_unknown(api: Api) -> None:
    r = api.create(IDLE)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "session_not_active"
    r = api.create(UNKNOWN)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"

    denial = api.denied(MISSING_TERMINAL, IDLE)
    assert denial.status_code == 409
    assert denial.json()["error"]["code"] == "session_not_active"
    denial = api.denied(MISSING_TERMINAL, UNKNOWN)
    assert denial.status_code == 404
    assert denial.json()["error"]["code"] == "not_found"

    # A terminal that was created while its session was active is unreachable once it stops.
    terminal_id = api.new()
    api.host.active.discard(SID)
    denial = api.denied(terminal_id)
    assert denial.status_code == 409
    assert denial.json()["error"]["code"] == "session_not_active"
    api.host.active.add(SID)

    denial = api.denied(MISSING_TERMINAL)
    assert denial.status_code == 404
    assert denial.json()["error"]["code"] == "terminal_not_found"

    for r in (api.list(UNKNOWN), api.delete(MISSING_TERMINAL, UNKNOWN)):
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"


BAD_IDS = [
    "..%2F..%2Fetc%2Fpasswd",
    "..%2F",
    "term_zz",
    "term_ABCDEF012345",
    "TERM_abcdef012345",
    "term_abcdef0123456",
    "term_abcdef01234",
    "term_abcdef01234%00",
    "x",
]


def test_invalid_terminal_id_rejected(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    def never(*_: Any, **__: Any) -> Any:
        raise AssertionError("a malformed terminal id reached the registry")

    monkeypatch.setattr(api.registry, "close", never)
    monkeypatch.setattr(api.registry, "attach", never)
    for bad in BAD_IDS:
        r = api.delete(bad)
        assert r.status_code == 422, (bad, r.text)
        assert r.json()["error"]["code"] == "invalid_request", bad
        denial = api.denied(bad)
        assert denial.status_code == 422, (bad, denial.text)
        assert denial.json()["error"]["code"] == "invalid_request", bad


# ------------------------------------------------------------------ WebSocket


def test_attach_round_trip(api: Api) -> None:
    terminal_id = api.new()
    wait_for(lambda: api.ring(terminal_id) != b"")  # the prompt
    before = api.ring(terminal_id)
    with api.attach(terminal_id) as ws:
        replay = read_until(ws, before)
        assert replay.startswith(before)
        wait_for(lambda: api.info(terminal_id)["attached"] == 1)
        type_line(ws, "echo RT$((1000+234))")
        assert b"RT1234" in read_until(ws, b"RT1234")
        type_line(ws, "pwd")
        assert str(api.host.workdirs[SID]).encode() in read_until(
            ws, str(api.host.workdirs[SID]).encode()
        )
    wait_for(lambda: api.info(terminal_id)["attached"] == 0)
    assert api.info(terminal_id)["alive"] is True


def test_control_frames(api: Api) -> None:
    terminal_id = api.new()
    with api.attach(terminal_id) as ws:
        ws.send_text(json.dumps({"type": "resize", "cols": 101, "rows": 41}))
        type_line(ws, "stty size")
        assert b"41 101" in read_until(ws, b"41 101")
        assert (api.info(terminal_id)["rows"], api.info(terminal_id)["cols"]) == (41, 101)
        ws.send_text(json.dumps({"type": "ping"}))
        assert next_control(ws) == {"type": "pong"}
        ws.send_text(json.dumps({"type": "launch"}))
        assert close_code(ws) == 1003
    for unknown in ("not json", "[1, 2]", json.dumps({"kind": "ping"}), '"ping"'):
        with api.attach(terminal_id) as ws:
            ws.send_text(unknown)
            assert close_code(ws) == 1003, unknown
    assert api.info(terminal_id)["alive"] is True


def test_bad_resize_ignored(api: Api) -> None:
    terminal_id = api.new(rows=20, cols=70)
    bad = [
        {"type": "resize", "cols": 0, "rows": 10},
        {"type": "resize", "cols": 1001, "rows": 10},
        {"type": "resize", "cols": 80, "rows": -1},
        {"type": "resize", "cols": "80", "rows": 24},
        {"type": "resize", "cols": 80.5, "rows": 24},
        {"type": "resize", "cols": True, "rows": 24},
        {"type": "resize", "rows": 24},
        {"type": "resize"},
    ]
    with api.attach(terminal_id) as ws:
        for frame in bad:
            ws.send_text(json.dumps(frame))
        ws.send_text(json.dumps({"type": "ping"}))
        assert next_control(ws) == {"type": "pong"}  # still open, nothing closed it
        type_line(ws, "stty size")
        assert b"20 70" in read_until(ws, b"20 70")
    assert (api.info(terminal_id)["rows"], api.info(terminal_id)["cols"]) == (20, 70)


def test_frame_cap_and_exit(api: Api) -> None:
    terminal_id = api.new()
    with api.attach(terminal_id) as ws:
        ws.send_bytes(b"x" * MAX_FRAME)  # exactly at the cap: accepted
        ws.send_text(json.dumps({"type": "ping"}))
        assert next_control(ws) == {"type": "pong"}
        ws.send_bytes(b"x" * (MAX_FRAME + 1))
        assert close_code(ws) == 1009
    with api.attach(terminal_id) as ws:
        ws.send_text(json.dumps({"type": "ping", "pad": "p" * MAX_FRAME}))
        assert close_code(ws) == 1009
    assert api.info(terminal_id)["alive"] is True

    with api.attach(terminal_id) as ws:
        ws.send_bytes(b"\x15")  # drop the junk line typed above
        type_line(ws, "exit 3")
        assert next_control(ws) == {"type": "exit", "code": 3}
        assert close_code(ws) == 1000
    info = api.info(terminal_id)
    assert (info["alive"], info["exit_code"]) == (False, 3)


def test_attach_exited_replays_and_closes(api: Api) -> None:
    terminal_id = api.new()
    with api.attach(terminal_id) as ws:
        type_line(ws, "echo EX$((40+2)); exit 7")
        assert next_control(ws) == {"type": "exit", "code": 7}
        assert close_code(ws) == 1000
    with api.attach(terminal_id) as ws:
        replay = b""
        while True:
            msg = recv(ws)
            assert msg["type"] == "websocket.send", msg
            if msg.get("text") is not None:
                assert json.loads(msg["text"]) == {"type": "exit", "code": 7}
                break
            replay += msg["bytes"]
        assert b"EX42" in replay
        assert close_code(ws) == 1000


def test_reattach_replays(api: Api) -> None:
    terminal_id = api.new()
    with api.attach(terminal_id) as ws:
        type_line(ws, "sleep 0.3; echo LATE$((7*11)); printf '%070000d\\n' 5")
    wait_for(lambda: api.info(terminal_id)["attached"] == 0)
    wait_for(lambda: b"LATE77" in api.ring(terminal_id) and b"00005" in api.ring(terminal_id))
    assert api.info(terminal_id)["alive"] is True
    with api.attach(terminal_id) as ws:
        frames: list[bytes] = []
        while b"00005" not in b"".join(frames):
            msg = recv(ws)
            assert msg["type"] == "websocket.send" and msg.get("bytes") is not None, msg
            frames.append(msg["bytes"])
        replay = b"".join(frames)
        assert b"LATE77" in replay
        assert len(replay) > terminals_api.SEND_CHUNK_BYTES
        assert all(len(f) <= terminals_api.SEND_CHUNK_BYTES for f in frames)
        type_line(ws, "echo AGAIN$((5+5))")
        assert b"AGAIN10" in read_until(ws, b"AGAIN10")


def test_overflowed_subscriber_closes_1013(api: Api) -> None:
    terminal_id = api.new()
    with api.attach(terminal_id) as ws:
        wait_for(lambda: api.info(terminal_id)["attached"] == 1)

        def overflow() -> None:
            for att in list(api.registry._terminals[terminal_id].subscribers):
                att._overflow()
                api.registry._unsubscribe(api.registry._terminals[terminal_id], att)

        api.client.portal.call(overflow)
        assert close_code(ws) == 1013
    assert api.info(terminal_id)["alive"] is True


def test_delete_while_attached_sends_exit(api: Api) -> None:
    terminal_id = api.new()
    with api.attach(terminal_id) as ws:
        wait_for(lambda: api.info(terminal_id)["attached"] == 1)
        assert api.delete(terminal_id).status_code == 204
        assert next_control(ws)["type"] == "exit"
        assert close_code(ws) == 1000
    assert api.denied(terminal_id).status_code == 404


class _LeavingSocket:
    """A client that disconnects at once; records what the server sends."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def receive(self) -> dict[str, Any]:
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    async def close(self, code: int) -> None:
        self.sent.append({"type": "websocket.close", "code": code})


class _QuietAttachment:
    """An attachment of a shell that prints nothing."""

    terminal_id = MISSING_TERMINAL
    snapshot = b""

    async def next(self) -> bytes:
        await anyio.sleep_forever()
        raise AssertionError("unreachable")


async def _cancel_after(scope: anyio.CancelScope, ticks: int) -> None:
    for _ in range(ticks):
        await asyncio.sleep(0)
    scope.cancel()


@pytest.mark.parametrize("ticks", range(8))
async def test_relay_cancellation_stays_with_the_canceller(ticks: int) -> None:
    """The client leaves and the host cancels the handler's scope right away (what the test
    client and a server shutdown do): the cancellation stays the scope's own, so no stray
    CancelledError escapes the scope, and the relay's loops are gone."""
    async with anyio.create_task_group() as tg:
        with anyio.CancelScope() as scope:
            tg.start_soon(_cancel_after, scope, ticks)
            socket, att = cast(Any, _LeavingSocket()), cast(Any, _QuietAttachment())
            relay = terminals_api._Relay(socket, att, MAX_FRAME)
            await relay.run()
            await anyio.sleep_forever()
    assert scope.cancelled_caught
    current = asyncio.current_task()
    assert {t for t in asyncio.all_tasks() if t is not current} == set()
