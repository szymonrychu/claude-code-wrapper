"""Hook context env: CCW_STATE_DIR and CCW_API_URL (K4), config dir agreement."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from conftest import World, wait_turn

from claude_code_wrapper.config import HooksConfig
from claude_code_wrapper.hooks import HookDispatcher
from claude_code_wrapper.manager import CreateSessionRequest
from claude_code_wrapper.redact import Redactor


def _dump(out: Path) -> dict:
    return {
        "type": "command",
        "blocking": True,
        "run": f'echo "$CCW_STATE_DIR|$CCW_API_URL|$CCW_CLAUDE_CONFIG_DIR|$CCW_EVENT" >> {out}',
    }


async def test_dispatcher_sets_state_dir_and_api_url_over_action_env(tmp_path):
    out = tmp_path / "o.txt"
    action = _dump(out)
    action["env"] = {"CCW_STATE_DIR": "evil", "CCW_API_URL": "evil"}
    d = HookDispatcher(
        HooksConfig.model_validate({"onTurn": [action]}),
        Redactor(),
        base_env={"PATH": os.environ["PATH"]},
        default_cwd=tmp_path,
        state_dir=tmp_path / "st",
        api_url="http://127.0.0.1:9",
    )
    await d.fire("onTurn", {"session_id": "s"})
    assert out.read_text().strip() == f"{tmp_path / 'st'}|http://127.0.0.1:9||onTurn"


async def test_dispatcher_defaults_to_empty_strings(tmp_path):
    out = tmp_path / "o.txt"
    d = HookDispatcher(
        HooksConfig.model_validate({"onTurn": [_dump(out)]}),
        Redactor(),
        base_env={"PATH": os.environ["PATH"], "CCW_STATE_DIR": "leak"},
        default_cwd=tmp_path,
    )
    await d.fire("onTurn", {"session_id": "s"})
    assert out.read_text().strip() == "|||onTurn"


def _probe(out: Path) -> dict:
    return {
        "type": "command",
        "blocking": True,
        "run": (
            'test -d "$CCW_STATE_DIR" && '
            'echo "$CCW_STATE_DIR|$CCW_API_URL|$CCW_CLAUDE_CONFIG_DIR|'
            f'$(cat "$CCW_STATE_DIR/keep" 2>/dev/null)" >> {out}'
        ),
    }


def _rows(out: Path) -> list[list[str]]:
    return [line.split("|") for line in out.read_text().strip().splitlines()]


async def test_state_dir_in_env_and_created(world: World):
    out = world.tmp / "env.txt"
    m = world.manager(world.cfg(hooks={"onCreate": [_probe(out)]}), api_url="http://127.0.0.1:4242")
    snap = await m.create(CreateSessionRequest())
    sid = snap["session_id"]
    state, api, _cfg, _keep = _rows(out)[0]
    assert state == str(world.session_dir(sid) / "hook-state")
    assert api == "http://127.0.0.1:4242"


async def test_state_dir_survives_stop_and_resume(world: World):
    out = world.tmp / "env.txt"
    cfg = world.cfg(hooks={"onCreate": [_probe(out)], "onResume": [_probe(out)]})
    m = world.manager(cfg)
    sid = (await m.create(CreateSessionRequest(prompt="hi")))["session_id"]
    await wait_turn(m, sid)
    state = world.session_dir(sid) / "hook-state"
    (state / "keep").write_text("kept")
    await m.destroy(sid)
    assert (state / "keep").read_text() == "kept"
    await m.resume(sid)
    rows = _rows(out)
    assert rows[-1][0] == str(state)
    assert rows[-1][3] == "kept"


async def test_state_dir_removed_on_purge(world: World):
    out = world.tmp / "env.txt"
    m = world.manager(world.cfg(hooks={"onCreate": [_probe(out)]}))
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    sdir = world.session_dir(sid)
    assert (sdir / "hook-state").is_dir()
    await m.destroy(sid, purge=True)
    assert not (sdir / "hook-state").exists()
    assert not sdir.exists()


async def test_hook_env_is_full_service_env(world: World, monkeypatch):
    monkeypatch.setenv("CCW_TEST_NOT_PASSTHROUGH", "secret-value")
    out = world.tmp / "full.txt"
    hook = {
        "type": "command",
        "blocking": True,
        "run": f'echo "$CCW_TEST_NOT_PASSTHROUGH" >> {out}',
    }
    m = world.manager(world.cfg(hooks={"onCreate": [hook]}))
    await m.create(CreateSessionRequest())
    assert out.read_text().strip() == "secret-value"


async def test_hook_and_claude_config_dir_match(world: World):
    out = world.tmp / "env.txt"
    m = world.manager(world.cfg(hooks={"onCreate": [_probe(out)]}))
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    ctx = m._active[sid]
    assert _rows(out)[0][2] == str(ctx.config_dir)
    assert _rows(out)[0][2] == m._child_env(ctx)["CLAUDE_CONFIG_DIR"]


async def test_api_url_is_bound_public_port(tmp_path: Path, monkeypatch):
    from claude_code_wrapper import service
    from claude_code_wrapper.config import Config
    from claude_code_wrapper.service import ServiceHandle, run_service

    seen = {}
    real = service.SessionManager

    class Spy(real):  # type: ignore[valid-type, misc]
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            seen["m"] = self

    monkeypatch.setattr(service, "SessionManager", Spy)
    cfg = Config.model_validate(
        {
            "server": {
                "host": "127.0.0.1",
                "port": 0,
                "internal_port": 0,
                "auth_token": "tok-abcdefgh",
            },
            "paths": {
                "workdir_root": str(tmp_path / "wd"),
                "sessions_root": str(tmp_path / "ss"),
            },
            "limits": {"shutdown_timeout_seconds": 2},
        }
    )
    ready, stop, handle = asyncio.Event(), asyncio.Event(), ServiceHandle()
    task = asyncio.create_task(run_service(cfg, ready=ready, stop=stop, handle=handle))
    await asyncio.wait_for(ready.wait(), 15)
    assert handle.public_port > 0
    assert seen["m"].api_url == f"http://127.0.0.1:{handle.public_port}"
    stop.set()
    await asyncio.wait_for(task, 10)
