from __future__ import annotations

import asyncio
import io
import json
import logging
from pathlib import Path

import httpx
import pytest

from claude_code_wrapper.config import Config
from claude_code_wrapper.redact import Redactor
from claude_code_wrapper.service import (
    JsonFormatter,
    RedactingFilter,
    ServiceHandle,
    run_service,
)
from claude_code_wrapper.tooling import ToolingError


def make_cfg(tmp_path: Path, **extra) -> Config:
    data = {
        "server": {
            "host": "127.0.0.1",
            "port": 0,
            "internal_port": 0,
            "auth_token": "tok-abcdefgh",
        },
        "paths": {"workdir_root": str(tmp_path / "wd"), "sessions_root": str(tmp_path / "ss")},
        "limits": {"shutdown_timeout_seconds": 2},
    }
    data.update(extra)
    return Config.model_validate(data)


async def start(cfg: Config):
    ready, stop, handle = asyncio.Event(), asyncio.Event(), ServiceHandle()
    task = asyncio.create_task(run_service(cfg, ready=ready, stop=stop, handle=handle))
    waiter = asyncio.create_task(ready.wait())
    await asyncio.wait({task, waiter}, timeout=15, return_when=asyncio.FIRST_COMPLETED)
    if task.done():
        task.result()
    assert ready.is_set()
    return task, stop, handle


async def test_serves_both_ports_and_stops_cleanly(tmp_path: Path) -> None:
    task, stop, handle = await start(make_cfg(tmp_path))
    assert handle.public_port > 0 and handle.internal_port > 0
    assert handle.public_port != handle.internal_port
    async with httpx.AsyncClient() as c:
        pub = f"http://127.0.0.1:{handle.public_port}"
        assert (await c.get(f"{pub}/healthz")).status_code == 200
        assert (await c.get(f"{pub}/readyz")).status_code == 200
        assert (await c.get(f"{pub}/v1/sessions")).status_code == 401
        r = await c.get(f"{pub}/v1/sessions", headers={"Authorization": "Bearer tok-abcdefgh"})
        assert r.status_code == 200
        m = await c.get(f"{pub}/metrics")
        assert b"ccw_sessions_active" in m.content
        r = await c.get(f"http://127.0.0.1:{handle.internal_port}/internal/healthz")
        assert r.status_code == 200
        # the internal routes are not on the public port
        assert (await c.get(f"{pub}/internal/healthz")).status_code == 404
    stop.set()
    await asyncio.wait_for(task, 10)
    async with httpx.AsyncClient() as c:
        with pytest.raises(httpx.TransportError):
            await c.get(f"http://127.0.0.1:{handle.public_port}/healthz")


async def test_port_zero_handle_and_internal_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_code_wrapper import service

    seen = {}
    real = service.SessionManager

    class Spy(real):  # type: ignore[valid-type, misc]
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            seen["m"] = self

    monkeypatch.setattr(service, "SessionManager", Spy)
    task, stop, handle = await start(make_cfg(tmp_path))
    assert seen["m"].internal_url == f"http://127.0.0.1:{handle.internal_port}"
    assert seen["m"].started is True
    stop.set()
    await asyncio.wait_for(task, 10)


async def test_readyz_only_after_init_and_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_code_wrapper import service

    gate = asyncio.Event()
    real = service.SessionManager.load_persisted

    async def slow(self):
        await gate.wait()
        await real(self)

    monkeypatch.setattr(service.SessionManager, "load_persisted", slow)
    ready, stop, handle = asyncio.Event(), asyncio.Event(), ServiceHandle()
    task = asyncio.create_task(
        run_service(make_cfg(tmp_path), ready=ready, stop=stop, handle=handle)
    )
    await asyncio.sleep(0.3)
    assert not ready.is_set()
    gate.set()
    await asyncio.wait_for(ready.wait(), 10)
    async with httpx.AsyncClient() as c:
        r = await c.get(f"http://127.0.0.1:{handle.public_port}/readyz")
    assert r.status_code == 200
    stop.set()
    await asyncio.wait_for(task, 10)


async def test_required_mise_failure_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # no mise binary
    cfg = make_cfg(
        tmp_path,
        mise={
            "tools": {"python": "3.12"},
            "required": True,
            "global_config_path": str(tmp_path / "g/config.toml"),
        },
    )
    with pytest.raises(ToolingError):
        await run_service(cfg, ready=asyncio.Event(), stop=asyncio.Event())


async def test_non_required_mise_failure_still_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    cfg = make_cfg(
        tmp_path,
        mise={
            "tools": {"python": "3.12"},
            "required": False,
            "global_config_path": str(tmp_path / "g/config.toml"),
        },
    )
    task, stop, _ = await start(cfg)
    stop.set()
    await asyncio.wait_for(task, 10)


async def test_shutdown_not_held_by_open_sse(tmp_path: Path) -> None:
    task, stop, handle = await start(make_cfg(tmp_path))
    pub = f"http://127.0.0.1:{handle.public_port}"
    auth = {"Authorization": "Bearer tok-abcdefgh"}
    async with httpx.AsyncClient(timeout=None) as c:
        created = await c.post(f"{pub}/v1/sessions", json={}, headers=auth)
        # creation may fail (no claude): the point is shutdown time with a client attached
        sid = created.json().get("id") or created.json().get("session_id")
        stream = None
        if sid:
            stream = c.stream("GET", f"{pub}/v1/sessions/{sid}/events", headers=auth)
            resp = await stream.__aenter__()
            assert resp.status_code in (200, 404)
        stop.set()
        await asyncio.wait_for(task, 15)
        if stream is not None:
            await stream.__aexit__(None, None, None)


def test_json_formatter_and_redacting_filter() -> None:
    red = Redactor(["supersecretvalue"])
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter(red))
    lg = logging.getLogger("t.json")
    lg.propagate = False
    lg.addHandler(handler)
    lg.setLevel(logging.INFO)
    lg.info("hello %s", "supersecretvalue", extra={"session": "s1", "tok": "supersecretvalue"})
    line = json.loads(buf.getvalue())
    assert line["level"] == "info" and line["logger"] == "t.json"
    assert "ts" in line
    assert line["msg"] == "hello ***"
    assert line["session"] == "s1" and line["tok"] == "***"
    assert "supersecretvalue" not in buf.getvalue()


async def test_run_service_binds_public_and_internal_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_code_wrapper import service

    hosts = []
    real = service._bind

    def spy(host: str, port: int):
        hosts.append(host)
        return real(host, port)

    monkeypatch.setattr(service, "_bind", spy)
    task, stop, handle = await start(make_cfg(tmp_path))
    try:
        assert hosts == ["127.0.0.1", "127.0.0.1"]
        for port in (handle.public_port, handle.internal_port):
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


async def test_sigterm_shuts_down_sessions_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import signal
    import time

    from claude_code_wrapper import service

    calls = []
    real = service.SessionManager.shutdown

    async def spy(self):
        t0 = time.monotonic()
        await real(self)
        calls.append(time.monotonic() - t0)

    monkeypatch.setattr(service.SessionManager, "shutdown", spy)
    ready, handle = asyncio.Event(), ServiceHandle()
    cfg = make_cfg(tmp_path)
    task = asyncio.create_task(run_service(cfg, ready=ready, handle=handle))
    await asyncio.wait_for(ready.wait(), 15)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, cfg.limits.shutdown_timeout_seconds + 5)
    assert len(calls) == 1 and calls[0] <= cfg.limits.shutdown_timeout_seconds + 1


async def test_logs_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    from claude_code_wrapper import service

    real = service.SessionManager.load_persisted

    async def noisy(self):
        self.redactor.add(["hunter2-secret-value"])
        logging.getLogger("t.noisy").warning("leak %s", "hunter2-secret-value")
        await real(self)

    monkeypatch.setattr(service.SessionManager, "load_persisted", noisy)
    task, stop, _ = await start(make_cfg(tmp_path))
    stop.set()
    await asyncio.wait_for(task, 10)
    out = capsys.readouterr().out
    assert "hunter2-secret-value" not in out
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert any(line["msg"] == "leak ***" for line in lines)
    assert all({"ts", "level", "logger", "msg"} <= line.keys() for line in lines)


async def test_shutdown_grace_from_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from claude_code_wrapper import service

    seen = []
    real = service.SessionManager.shutdown

    async def spy(self):
        seen.append(self.cfg.limits.shutdown_timeout_seconds)
        await real(self)

    monkeypatch.setattr(service.SessionManager, "shutdown", spy)
    cfg = make_cfg(tmp_path)
    cfg.limits.shutdown_timeout_seconds = 7
    task, stop, _ = await start(cfg)
    stop.set()
    await asyncio.wait_for(task, 10)
    assert seen == [7]
