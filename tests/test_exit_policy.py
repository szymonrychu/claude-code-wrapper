"""K8 exit policy (D12): exit_when_last_destroyed, exit_when_dead, on_exit_request wiring."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from conftest import World, jsonl, wait_until

from claude_code_wrapper import manager as manager_mod
from claude_code_wrapper.config import Config
from claude_code_wrapper.manager import CreateSessionRequest
from claude_code_wrapper.service import ServiceHandle, run_service

CRASH = {"turns": [{"match": "crash", "steps": [{"exit": 1}]}]}


def test_defaults_off() -> None:
    cfg = Config.model_validate({})
    assert cfg.sessions.exit_when_last_destroyed is False
    assert cfg.sessions.exit_when_dead is False


async def test_last_destroy_stops_service_exit_0(world: World) -> None:
    codes: list[int] = []
    m = world.manager(
        world.cfg(sessions={"exit_when_last_destroyed": True}), on_exit_request=codes.append
    )
    a = (await m.create(CreateSessionRequest()))["session_id"]
    b = (await m.create(CreateSessionRequest()))["session_id"]
    await m.destroy(a)
    assert codes == []
    await m.destroy(b)
    assert codes == [0]


async def test_no_exit_before_any_session(world: World) -> None:
    codes: list[int] = []
    m = world.manager(
        world.cfg(sessions={"exit_when_last_destroyed": True, "idle_timeout_seconds": 0.3}),
        on_exit_request=codes.append,
        reaper_interval=0.05,
    )
    await m.load_persisted()
    await asyncio.sleep(0.4)
    assert codes == []


async def test_idle_reaper_emptying_triggers_exit(world: World) -> None:
    codes: list[int] = []
    m = world.manager(
        world.cfg(sessions={"exit_when_last_destroyed": True, "idle_timeout_seconds": 0.4}),
        on_exit_request=codes.append,
        reaper_interval=0.05,
    )
    await m.load_persisted()
    await m.create(CreateSessionRequest())
    await wait_until(lambda: codes == [0], timeout=10)


async def test_shutdown_does_not_trigger(world: World) -> None:
    codes: list[int] = []
    m = world.manager(
        world.cfg(sessions={"exit_when_last_destroyed": True}), on_exit_request=codes.append
    )
    await m.create(CreateSessionRequest())
    await m.shutdown()
    assert codes == []


async def test_defaults_keep_service_running(world: World) -> None:
    codes: list[int] = []
    m = world.manager(world.cfg(), on_exit_request=codes.append)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    await m.destroy(sid)
    assert codes == []


async def test_dead_session_exits_3_and_writes_termination_log(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "termination-log"
    monkeypatch.setattr(manager_mod, "TERMINATION_LOG", str(log))
    codes: list[int] = []
    m = world.manager(
        world.cfg(CRASH, sessions={"max_restarts": 0, "exit_when_dead": True}),
        on_exit_request=codes.append,
    )
    sid = (await m.create(CreateSessionRequest(prompt="crash now")))["session_id"]
    await wait_until(lambda: codes == [3], timeout=15)
    assert log.read_text() == f"session {sid} dead: claude exited"


async def test_dead_skips_unwritable_termination_log(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager_mod, "TERMINATION_LOG", str(tmp_path / "no" / "such" / "log"))
    codes: list[int] = []
    m = world.manager(
        world.cfg(CRASH, sessions={"max_restarts": 0, "exit_when_dead": True}),
        on_exit_request=codes.append,
    )
    await m.create(CreateSessionRequest(prompt="crash now"))
    await wait_until(lambda: codes == [3], timeout=15)


async def test_dead_without_flag_does_not_exit(world: World) -> None:
    codes: list[int] = []
    m = world.manager(world.cfg(CRASH, sessions={"max_restarts": 0}), on_exit_request=codes.append)
    sid = (await m.create(CreateSessionRequest(prompt="crash now")))["session_id"]
    await wait_until(lambda: m.get(sid)["state"] == "dead")
    await asyncio.sleep(0.2)
    assert codes == []


async def _serve(world: World, **sessions: object) -> tuple[ServiceHandle, asyncio.Task, str]:
    cfg = world.cfg(**sessions)
    cfg = cfg.model_copy(
        update={
            "server": cfg.server.model_copy(
                update={
                    "host": "127.0.0.1",
                    "port": 0,
                    "internal_port": 0,
                    "auth_token": "tok-abcdefgh",
                }
            )
        }
    )
    handle = ServiceHandle()
    ready = asyncio.Event()
    task = asyncio.create_task(run_service(cfg, ready=ready, handle=handle))
    await asyncio.wait_for(ready.wait(), 20)
    return handle, task, f"http://127.0.0.1:{handle.public_port}"


_AUTH = {"Authorization": "Bearer tok-abcdefgh"}


async def test_last_destroy_delete_response_complete_before_exit(
    world: World, tmp_path: Path
) -> None:
    out = tmp_path / "on_destroy.jsonl"
    hook = {"type": "command", "run": f"cat >> '{out}'; echo >> '{out}'"}
    handle, task, pub = await _serve(
        world, sessions={"exit_when_last_destroyed": True}, hooks={"onDestroy": [hook]}
    )
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{pub}/v1/sessions", json={}, headers=_AUTH)
        assert r.status_code == 201
        sid = r.json()["session_id"]
        d = await c.delete(f"{pub}/v1/sessions/{sid}", headers=_AUTH)
        assert d.status_code == 200
        body = d.json()
        assert body["session_id"] == sid
        assert body["state"] == "stopped"
        if "hook_outputs" in body:
            assert isinstance(body["hook_outputs"], list | dict)
    await asyncio.wait_for(task, 20)
    assert handle.exit_code == 0


async def test_shutdown_drains_on_destroy_before_exit(world: World, tmp_path: Path) -> None:
    out = tmp_path / "on_destroy.jsonl"
    hook = {"type": "command", "run": f"cat >> '{out}'; echo >> '{out}'"}
    handle, task, pub = await _serve(
        world, sessions={"exit_when_last_destroyed": True}, hooks={"onDestroy": [hook]}
    )
    async with httpx.AsyncClient(timeout=20) as c:
        a = (await c.post(f"{pub}/v1/sessions", json={}, headers=_AUTH)).json()["session_id"]
        b = (await c.post(f"{pub}/v1/sessions", json={}, headers=_AUTH)).json()["session_id"]
        await c.delete(f"{pub}/v1/sessions/{a}", headers=_AUTH)
        await c.delete(f"{pub}/v1/sessions/{b}", headers=_AUTH)
    await asyncio.wait_for(task, 20)
    assert handle.exit_code == 0
    assert sorted(p["session_id"] for p in jsonl(out)) == sorted([a, b])


def test_first_exit_request_wins() -> None:
    handle = ServiceHandle()
    assert handle.exit_code is None
    assert handle.request_exit(3) is True
    assert handle.request_exit(0) is False
    assert handle.exit_code == 3


def test_cli_serve_returns_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from claude_code_wrapper import cli

    async def fake_run(cfg, *, handle=None, **kw):
        handle.request_exit(3)

    monkeypatch.setattr(cli, "run_service", fake_run)
    monkeypatch.setattr(cli, "missing_prerequisites", lambda binary: [])
    conf = tmp_path / "c.yaml"
    conf.write_text("server:\n  auth_token: tok-abcdefgh\n")
    assert cli.main(["serve", "--config", str(conf)]) == 3
