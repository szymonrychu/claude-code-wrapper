"""The onStart instance hook: once per ccw process start, before peers, resume and listening."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from claude_code_wrapper import service
from claude_code_wrapper.config import HOOK_EVENTS, VETO_EVENTS, Config, HooksConfig
from claude_code_wrapper.hooks import EVENT_DECISIONS, INSTANCE_EVENTS
from claude_code_wrapper.service import ServiceHandle, run_service


def cmd(run: str, **kw):
    return {"type": "command", "run": run, **kw}


def make_cfg(tmp_path: Path, on_start: list, **extra) -> Config:
    data = {
        "server": {
            "host": "127.0.0.1",
            "port": 0,
            "internal_port": 0,
            "auth_token": "tok-abcdefgh",
        },
        "paths": {"workdir_root": str(tmp_path / "wd"), "sessions_root": str(tmp_path / "ss")},
        "limits": {"shutdown_timeout_seconds": 2},
        "hooks": {"onStart": on_start},
    }
    data.update(extra)
    return Config.model_validate(data)


async def start(cfg: Config):
    ready, stop, handle = asyncio.Event(), asyncio.Event(), ServiceHandle()
    task = asyncio.create_task(run_service(cfg, ready=ready, stop=stop, handle=handle))
    waiter = asyncio.create_task(ready.wait())
    await asyncio.wait({task, waiter}, timeout=15, return_when=asyncio.FIRST_COMPLETED)
    return task, stop, handle, ready


async def stop_service(task, stop) -> None:
    stop.set()
    await asyncio.wait_for(task, 10)


def test_config_accepts_on_start_and_rejects_matcher():
    assert "onStart" in HOOK_EVENTS and "onStart" in VETO_EVENTS
    assert "onStart" in INSTANCE_EVENTS
    assert EVENT_DECISIONS["onStart"] == ("allow", "deny")
    assert len(HooksConfig.model_validate({"onStart": [cmd("true")]}).onStart) == 1
    with pytest.raises(ValidationError):
        HooksConfig.model_validate({"onStart": [cmd("true", matcher="Bash")]})
    with pytest.raises(ValidationError):
        HooksConfig.model_validate({"onStart": [cmd("true", interval_seconds=5)]})


async def test_fires_before_peers_and_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    order = tmp_path / "order"
    cfg = make_cfg(tmp_path, [cmd(f"echo hook >> {order}", blocking=True)])
    real = service.SessionManager

    class Spy(real):  # type: ignore[misc, valid-type]
        async def start_peers(self, port):
            with order.open("a") as f:
                f.write("peers\n")
            return await super().start_peers(port)

        async def load_persisted(self):
            with order.open("a") as f:
                f.write("resume\n")
            return await super().load_persisted()

    monkeypatch.setattr(service, "SessionManager", Spy)
    task, stop, _, ready = await start(cfg)
    assert ready.is_set()
    await stop_service(task, stop)
    assert order.read_text().split() == ["hook", "peers", "resume"]


async def test_blocking_deny_exits_non_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    marker = tmp_path / "bound"
    script = 'echo \'{"decision":"deny","reason":"nope"}\''
    cfg = make_cfg(tmp_path, [cmd(script, blocking=True)])
    task, _, handle, ready = await start(cfg)
    await asyncio.wait_for(task, 10)
    assert not ready.is_set()
    assert handle.exit_code == 1
    assert handle.public_port == 0 and not marker.exists()
    assert "onStart denied: nope" in capsys.readouterr().out


async def test_on_error_deny_exits_and_allow_continues(tmp_path: Path):
    cfg = make_cfg(tmp_path, [cmd("exit 3", blocking=True, on_error="deny")])
    task, _, handle, ready = await start(cfg)
    await asyncio.wait_for(task, 10)
    assert handle.exit_code == 1 and not ready.is_set()

    cfg = make_cfg(tmp_path, [cmd("exit 3", blocking=True, on_error="allow")])
    task, stop, handle, ready = await start(cfg)
    assert ready.is_set() and handle.exit_code is None
    await stop_service(task, stop)

    cfg = make_cfg(tmp_path, [cmd("sleep 30", blocking=True, on_error="deny", timeout_seconds=1)])
    task, _, handle, ready = await start(cfg)
    await asyncio.wait_for(task, 15)
    assert handle.exit_code == 1 and not ready.is_set()


async def test_non_blocking_does_not_delay_startup(tmp_path: Path):
    cfg = make_cfg(tmp_path, [cmd("sleep 5", blocking=False)])
    t0 = time.monotonic()
    task, stop, _, ready = await start(cfg)
    assert ready.is_set()
    assert time.monotonic() - t0 < 4
    await stop_service(task, stop)


async def test_command_gets_instance_envelope_in_sessions_root(tmp_path: Path):
    out = tmp_path / "env.json"
    cfg = make_cfg(tmp_path, [cmd(f"cat > {out}; pwd > {tmp_path}/cwd", blocking=True)])
    task, stop, handle, ready = await start(cfg)
    assert ready.is_set()
    async with httpx.AsyncClient() as c:
        r = await c.get(f"http://127.0.0.1:{handle.public_port}/healthz")
        assert r.status_code == 200
    await stop_service(task, stop)
    env = json.loads(out.read_text())
    assert env["event"] == "onStart"
    assert env["session_id"] == "" and env["workdir"] == ""
    assert env["claude_session_id"] is None and env["turn_id"] is None
    assert env["data"] == {}
    assert Path((tmp_path / "cwd").read_text().strip()).resolve() == (tmp_path / "ss").resolve()
