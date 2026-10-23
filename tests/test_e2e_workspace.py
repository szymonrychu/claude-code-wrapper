"""End to end: the workspace surface (files and terminals) through the public `/v1` API only.

Headless parity (epic item H): everything the browser workspace does with a wrapper is done
here by a bare HTTP and WebSocket client against the real `run_service` on loopback ports with
bearer auth on and the scripted fake claude standing in for claude. No internal module is
reached: the test writes into the session workdir (the one thing a file browser would show
the changes of) and otherwise speaks only `/v1`.

The secret scrub is asserted against a service seeded with a sentinel for every scrubbed name:
each secret-kind `ClaudeEnv` name is set in the service's own env (reaching claude through a
`*` passthrough) and in the session `env`, and `CCW_AUTH_TOKEN`, `CCW_PEER_TOKEN` and
`CCW_HOOK_TOKEN` are set in the service env, so an absence in the shell is meaningful.

Every wait is bounded: a hung step fails the test with what it was waiting for.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidStatus

from claude_code_wrapper.claude_env import ClaudeEnv
from claude_code_wrapper.terminals import SCRUBBED_ENV

AUTH_TOKEN = "tok-e2e-workspace-0123456789"
AUTH = {"Authorization": f"Bearer {AUTH_TOKEN}"}
DEADLINE = 15.0
PASSTHROUGH_NAME = "E2E_VISIBLE_NOTE"
PASSTHROUGH_VALUE = "visible-passthrough-value"
SENTINEL_PREFIX = "sentinel-secret-"

#: Secret-kind `ClaudeEnv` variables, by environment name and by session `env` field name.
SECRET_SPECS = [s for s in ClaudeEnv.catalogue() if s.secret]
SERVICE_ONLY = ("CCW_AUTH_TOKEN", "CCW_PEER_TOKEN", "CCW_HOOK_TOKEN")


def sentinel(name: str) -> str:
    return f"{SENTINEL_PREFIX}{name.lower().replace('_', '-')}-0123456789"


def ws_url(base: str, sid: str, terminal_id: str) -> str:
    return f"ws{base.removeprefix('http')}/v1/sessions/{sid}/terminals/{terminal_id}/attach"


class Shell:
    """One attached terminal socket: accumulates PTY bytes and control frames, bounded waits."""

    def __init__(self, ws: ClientConnection) -> None:
        self.ws = ws
        self.output = b""
        self.controls: list[dict[str, Any]] = []

    @property
    def text(self) -> str:
        return self.output.decode(errors="replace")

    async def _pump(self, until: Callable[[], bool], what: str) -> None:
        end = time.monotonic() + DEADLINE
        while not until():
            left = end - time.monotonic()
            try:
                if left <= 0:
                    raise TimeoutError
                message = await asyncio.wait_for(self.ws.recv(), left)
            except TimeoutError:
                raise AssertionError(
                    f"timed out after {DEADLINE:g}s waiting for {what}; "
                    f"output tail: {self.text[-400:]!r}; controls: {self.controls}"
                ) from None
            if isinstance(message, bytes):
                self.output += message
            else:
                self.controls.append(json.loads(message))

    async def wait_text(self, needle: str) -> None:
        await self._pump(lambda: needle in self.text, f"terminal output {needle!r}")

    async def wait_control(self, kind: str) -> dict[str, Any]:
        await self._pump(
            lambda: any(c.get("type") == kind for c in self.controls), f"a {kind!r} frame"
        )
        return next(c for c in self.controls if c.get("type") == kind)

    async def send(self, line: str) -> None:
        await self.ws.send(line.encode())

    async def control(self, frame: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(frame))


async def attach(base: str, sid: str, terminal_id: str, headers: dict[str, str]) -> Shell:
    ws = await asyncio.wait_for(
        connect(ws_url(base, sid, terminal_id), additional_headers=headers), DEADLINE
    )
    return Shell(ws)


async def denied_status(base: str, sid: str, terminal_id: str, headers: dict[str, str]) -> int:
    """The HTTP status of a refused upgrade; fails if the socket opens."""
    try:
        ws = await asyncio.wait_for(
            connect(ws_url(base, sid, terminal_id), additional_headers=headers), DEADLINE
        )
    except InvalidStatus as exc:
        return exc.response.status_code
    await ws.close()
    raise AssertionError(f"upgrade for {terminal_id} was accepted, expected a denial")


@pytest.fixture
async def workspace(world, ccw_service, monkeypatch):
    """A running service and one session, seeded so a leaked secret would show in a shell."""
    monkeypatch.setenv(PASSTHROUGH_NAME, PASSTHROUGH_VALUE)
    for name in [s.env for s in SECRET_SPECS] + list(SERVICE_ONLY):
        monkeypatch.setenv(name, sentinel(name))
    cfg = world.service_cfg(
        "ws",
        claude={"passthrough_env": ["*"]},
        server={"auth_token": AUTH_TOKEN},
    )
    svc = await ccw_service(cfg)
    session_env = {s.field: sentinel(s.env) for s in SECRET_SPECS}
    async with httpx.AsyncClient(base_url=svc.url, headers=AUTH, timeout=DEADLINE) as client:
        r = await client.post("/v1/sessions", json={"env": session_env})
        assert r.status_code == 201, r.text
        sid = r.json()["session_id"]

        async def ready() -> bool:
            return (await client.get(f"/v1/sessions/{sid}")).json()["state"] == "ready"

        end = time.monotonic() + DEADLINE
        while not await ready():
            assert time.monotonic() < end, f"timed out after {DEADLINE:g}s waiting for ready"
            await asyncio.sleep(0.05)
        yield svc, client, sid


async def workdir_of(client: httpx.AsyncClient, sid: str) -> Path:
    """The session's workdir as the public API reports it."""
    return Path((await client.get(f"/v1/sessions/{sid}")).json()["workdir"])


async def test_files_over_real_service(workspace) -> None:
    svc, client, sid = workspace
    workdir = await workdir_of(client, sid)
    (workdir / "b.txt").write_text("second file\n")
    (workdir / "a.txt").write_text("first file\n")
    (workdir / "src").mkdir()
    (workdir / "src" / "main.py").write_text("print('hi')\n")

    r = await client.get(f"/v1/sessions/{sid}/files")
    assert r.status_code == 200, r.text
    listing = r.json()
    entries = {e["name"]: e["type"] for e in listing["entries"]}
    assert entries["src"] == "dir" and entries["a.txt"] == "file" and entries["b.txt"] == "file"
    names = [e["name"] for e in listing["entries"]]
    assert names.index("src") < names.index("a.txt") < names.index("b.txt"), names
    assert listing["entries"][0]["type"] == "dir", "directories come first"

    sub = (await client.get(f"/v1/sessions/{sid}/files", params={"path": "src"})).json()
    assert [e["name"] for e in sub["entries"]] == ["main.py"]

    r = await client.get(f"/v1/sessions/{sid}/files/content", params={"path": "a.txt"})
    assert r.status_code == 200, r.text
    assert r.json()["content"] == "first file\n"

    for route in ("files", "files/content"):
        r = await client.get(f"/v1/sessions/{sid}/{route}", params={"path": "../.."})
        assert r.status_code == 400, r.text
        assert r.json()["error"]["code"] == "invalid_path", r.text


async def test_terminal_over_real_service(workspace) -> None:
    svc, client, sid = workspace
    workdir = await workdir_of(client, sid)
    base = f"/v1/sessions/{sid}/terminals"
    root = (await client.get(f"/v1/sessions/{sid}/files")).json()["root"]
    assert Path(root) == workdir.resolve()

    r = await client.post(base)
    assert r.status_code == 201, r.text
    terminal_id = r.json()["terminal_id"]
    listed = (await client.get(base)).json()["terminals"]
    assert [t["terminal_id"] for t in listed] == [terminal_id]

    shell = await attach(svc.url, sid, terminal_id, AUTH)
    try:
        await shell.send('echo "pwd:$PWD"; env; echo end-$((20+22))\n')
        await shell.wait_text("end-42")
        text = shell.text
        assert f"pwd:{root}" in text, text[-800:]
        for name in [s.env for s in SECRET_SPECS] + list(SERVICE_ONLY):
            assert name not in text, f"{name} leaked into the shell env"
            assert sentinel(name) not in text, f"the value seeded for {name} leaked"
        assert SENTINEL_PREFIX not in text
        assert AUTH_TOKEN not in text
        assert {s.env for s in SECRET_SPECS} | set(SERVICE_ONLY) >= SCRUBBED_ENV
        assert f"{PASSTHROUGH_NAME}={PASSTHROUGH_VALUE}" in text, "passthrough name missing"
        assert "TERM=xterm-256color" in text

        await shell.control({"type": "resize", "cols": 100, "rows": 30})
        await shell.send("stty size\n")
        await shell.wait_text("30 100")

        await shell.control({"type": "ping"})
        await shell.wait_control("pong")
    finally:
        await shell.ws.close()

    again = await attach(svc.url, sid, terminal_id, AUTH)
    try:
        await again.wait_text("end-42")
        assert f"pwd:{root}" in again.text, "replay lost the earlier output"
        assert "30 100" in again.text, "replay lost the resized stty output"
    finally:
        await again.ws.close()

    r = await client.delete(f"{base}/{terminal_id}")
    assert r.status_code == 204, r.text
    assert (await client.get(base)).json()["terminals"] == []
    assert await denied_status(svc.url, sid, terminal_id, AUTH) == 404


async def test_terminal_upgrade_needs_bearer(workspace) -> None:
    svc, client, sid = workspace
    r = await client.post(f"/v1/sessions/{sid}/terminals")
    assert r.status_code == 201, r.text
    terminal_id = r.json()["terminal_id"]
    assert await denied_status(svc.url, sid, terminal_id, {}) == 401
    wrong = {"Authorization": "Bearer not-the-token"}
    assert await denied_status(svc.url, sid, terminal_id, wrong) == 401
    # The refusal did not touch the terminal: a bearer-carrying attach still works.
    shell = await attach(svc.url, sid, terminal_id, AUTH)
    try:
        await shell.control({"type": "ping"})
        await shell.wait_control("pong")
    finally:
        await shell.ws.close()
