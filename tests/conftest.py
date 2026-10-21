"""Shared fixtures: the scripted fake claude, scenario files, a config dir and the ccw World."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import stat
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from claude_code_wrapper.config import Config
from claude_code_wrapper.manager import InvalidState, SessionManager, SessionNotFound
from claude_code_wrapper.redact import Redactor

FAKE_CLAUDE = Path(__file__).parent / "fake_claude" / "fake_claude.py"

BOOT = {
    "timeout_seconds": 5.0,
    "min_boot_seconds": 0.2,
    "idle_ready_seconds": 30.0,
    "submit_delay_seconds": 0.1,
    "dialogs": [
        {"pattern": "Bypass Permissions mode", "keys": ["\x1b[B", "\r"], "delay_seconds": 0.05}
    ],
}
FAKE_BOOT = {"bypass_dialog": True, "min_seconds": 0.1}


async def wait_until(
    pred: Callable[[], Any], timeout: float = 15.0, what: str = "condition"
) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out after {timeout}s waiting for: {what}")


def jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def turns_of(m: SessionManager, session_id: str) -> list:
    return m.open_turns(session_id).list()


async def wait_turn(m: SessionManager, session_id: str, state: str = "completed") -> Any:
    def done() -> Any:
        return [t for t in turns_of(m, session_id) if t.state == state]

    await wait_until(done)
    return done()[-1]


class World:
    """Config factory, managers under test, the loopback hook server and callback capture."""

    def __init__(self, tmp_path: Path, fake_bin: Path, scenario_file) -> None:
        self.tmp = tmp_path
        self.bin = fake_bin
        self.scenario_file = scenario_file
        self.log = tmp_path / "fake.log"
        self.managers: list[SessionManager] = []
        self.callbacks: list[dict] = []
        self.url = ""
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None

    # ---- loopback server

    def _manager_for(self, session_id: str) -> SessionManager | None:
        for m in reversed(self.managers):
            with contextlib.suppress(SessionNotFound, InvalidState):
                m.router(session_id)
                return m
        return None

    async def start_server(self) -> None:
        app = FastAPI()

        @app.post("/internal/hooks/{event}")
        async def hook(event: str, request: Request) -> JSONResponse:
            sid = request.headers.get("x-ccw-session", "")
            m = self._manager_for(sid)
            if m is None or not m.hook_token_matches(sid, request.headers.get("x-ccw-hook-token")):
                return JSONResponse({"error": "forbidden"}, status_code=403)
            out = await m.router(sid).handle_claude_hook(event, await request.json())
            return JSONResponse(dict(out))

        @app.post("/cb")
        async def callback(request: Request) -> JSONResponse:
            self.callbacks.append(await request.json())
            return JSONResponse({})

        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
        self._server = uvicorn.Server(config)
        self._task = asyncio.ensure_future(self._server.serve())
        await wait_until(lambda: self._server.started, 10)
        port = self._server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"

    async def close(self) -> None:
        for m in self.managers:
            with contextlib.suppress(Exception):
                await m.shutdown()
        if self._server is not None:
            self._server.should_exit = True
            assert self._task is not None
            await self._task

    # ---- config and managers

    def cfg(self, scenario: dict | None = None, **sections: Any) -> Config:
        scenario_path = self.scenario_file({"boot": FAKE_BOOT, **(scenario or {})})
        claude = sections.pop("claude", {})
        env = claude.pop("env", {})
        extra = {
            "FAKE_CLAUDE_SCENARIO": str(scenario_path),
            "FAKE_CLAUDE_LOG": str(self.log),
            **env.pop("extra", {}),
        }
        data: dict[str, Any] = {
            "server": {"callback_secret": "cb-secret-0123456789"},
            "paths": {
                "workdir_root": str(self.tmp / "workdir"),
                "sessions_root": str(self.tmp / "sessions"),
            },
            "claude": {
                "binary": str(self.bin),
                "boot": BOOT,
                **claude,
                "env": {**env, "extra": extra},
            },
            "mise": {"trusted_paths": [str(self.tmp / "workdir")]},
            "sessions": {"interrupt_settle_seconds": 2},
            "limits": {"shutdown_timeout_seconds": 2},
        }
        for key, value in sections.items():
            data[key] = {**data.get(key, {}), **value}
        return Config.model_validate(data)

    def manager(self, cfg: Config, **kw: Any) -> SessionManager:
        m = SessionManager(
            cfg,
            internal_url=self.url,
            service_env=dict(os.environ),
            redactor=kw.pop("redactor", None) or Redactor(),
            **kw,
        )
        self.managers.append(m)
        return m

    def session_dir(self, session_id: str) -> Path:
        return self.tmp / "sessions" / session_id

    def meta(self, session_id: str) -> dict:
        return json.loads((self.session_dir(session_id) / "session.json").read_text())

    def events(self, session_id: str) -> list[dict]:
        return jsonl(self.session_dir(session_id) / "events.jsonl")

    def interactive_argvs(self) -> list[list[str]]:
        return [e["argv"] for e in jsonl(self.log) if e["argv"][:1] != ["plugin"]]

    async def until(
        self, pred: Callable[[], Any], timeout: float = 15.0, what: str = "condition"
    ) -> None:
        """Bounded async poll; fails naming what it waited for (pred's name unless `what`)."""
        if what == "condition":
            what = getattr(pred, "__name__", "condition")
            if what == "<lambda>":
                with contextlib.suppress(OSError, TypeError):
                    what = inspect.getsource(pred).strip()
        await wait_until(pred, timeout, what)

    async def wait_turn(
        self, manager: SessionManager, session_id: str, state: str = "completed"
    ) -> Any:
        return await wait_turn(manager, session_id, state)


@pytest.fixture
async def world(tmp_path: Path, fake_claude_bin: Path, scenario_file):
    w = World(tmp_path, fake_claude_bin, scenario_file)
    await w.start_server()
    yield w
    await w.close()


@pytest.fixture
def fake_claude_bin(tmp_path: Path) -> Path:
    """An executable `claude` shim that runs the fake under the current interpreter."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    shim = bin_dir / "claude"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{FAKE_CLAUDE}" "$@"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


@pytest.fixture
def scenario_file(tmp_path: Path) -> Callable[[dict], Path]:
    """Factory: write a scenario dict to JSON and return the path."""
    counter = 0

    def make(scenario: dict) -> Path:
        nonlocal counter
        counter += 1
        path = tmp_path / f"scenario-{counter}.json"
        path.write_text(json.dumps(scenario))
        return path

    return make


@pytest.fixture
def claude_config_dir(tmp_path: Path) -> Path:
    path = tmp_path / "claude-config"
    path.mkdir()
    return path
