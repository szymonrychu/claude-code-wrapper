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

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from claude_code_wrapper.config import Config
from claude_code_wrapper.internal_api import create_internal_app
from claude_code_wrapper.manager import InvalidState, SessionManager, SessionNotFound
from claude_code_wrapper.redact import Redactor
from claude_code_wrapper.service import ServiceHandle, run_service

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
        self._internal: dict[int, httpx.AsyncClient] = {}

    # ---- loopback server

    def _manager_for(self, session_id: str) -> SessionManager | None:
        for m in reversed(self.managers):
            with contextlib.suppress(SessionNotFound, InvalidState):
                m.router(session_id)
                return m
        return None

    def _internal_client(self, m: SessionManager) -> httpx.AsyncClient:
        """One in-process client per manager onto its internal app (client address 127.0.0.1)."""
        if id(m) not in self._internal:
            transport = httpx.ASGITransport(app=create_internal_app(m), client=("127.0.0.1", 0))
            self._internal[id(m)] = httpx.AsyncClient(transport=transport, base_url="http://ccw")
        return self._internal[id(m)]

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

        @app.api_route("/internal/{path:path}", methods=["POST", "GET", "DELETE"])
        async def internal(path: str, request: Request) -> Response:
            m = self._manager_for(request.headers.get("x-ccw-session", ""))
            if m is None:
                return JSONResponse({"error": "forbidden"}, status_code=403)
            r = await self._internal_client(m).request(
                request.method,
                f"/internal/{path}",
                params=request.query_params.multi_items(),
                headers=[(k, v) for k, v in request.headers.items() if k != "host"],
                content=await request.body(),
            )
            skip = {"content-length", "transfer-encoding", "connection"}
            return Response(
                r.content,
                status_code=r.status_code,
                headers={k: v for k, v in r.headers.items() if k.lower() not in skip},
            )

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
        for client in self._internal.values():
            await client.aclose()
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

    def service_cfg(self, name: str, scenario: dict | None = None, **sections: Any) -> Config:
        """A `World.cfg` for one service: loopback, port 0, own roots under <tmp>/name."""
        root = self.tmp / name
        base = {
            "server": {
                "host": "127.0.0.1",
                "port": 0,
                "internal_port": 0,
                "auth_token": f"tok-{name}-0123456789",
            },
            "paths": {
                "workdir_root": str(root / "workdir"),
                "sessions_root": str(root / "sessions"),
            },
            "mise": {
                "trusted_paths": [str(root / "workdir")],
                "global_config_path": str(root / "mise" / "config.toml"),
            },
        }
        merged = {key: {**base[key], **sections.pop(key, {})} for key in base}
        return self.cfg(scenario, **merged, **sections)

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


class RunningService:
    """A `run_service` task on real loopback ports."""

    def __init__(self, task: asyncio.Task, stop_event: asyncio.Event, handle: ServiceHandle):
        self.task = task
        self._stop = stop_event
        self.public_port: int = handle.public_port
        self.internal_port: int = handle.internal_port
        self.url = f"http://127.0.0.1:{self.public_port}"

    async def stop(self) -> None:
        self._stop.set()
        await asyncio.wait_for(self.task, 15)


@pytest.fixture
async def ccw_service():
    """Factory `await ccw_service(cfg) -> RunningService`; every service is stopped at teardown."""
    started: list[RunningService] = []

    async def start(cfg: Config) -> RunningService:
        ready, stop, handle = asyncio.Event(), asyncio.Event(), ServiceHandle()
        task = asyncio.create_task(run_service(cfg, ready=ready, stop=stop, handle=handle))
        waiter = asyncio.create_task(ready.wait())
        await asyncio.wait({task, waiter}, timeout=15, return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        if task.done():
            task.result()
            raise AssertionError("service exited before it was ready")
        if not ready.is_set():
            task.cancel()
            raise AssertionError("service not ready within 15s")
        svc = RunningService(task, stop, handle)
        started.append(svc)
        return svc

    yield start
    for svc in started:
        await svc.stop()


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
