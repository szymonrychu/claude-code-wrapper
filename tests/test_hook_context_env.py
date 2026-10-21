"""Hook context env: CCW_STATE_DIR and CCW_API_URL (K4), config dir agreement."""

from __future__ import annotations

import os
from pathlib import Path

from conftest import World

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


async def test_state_dir_exists_before_on_create_and_survives_stop_until_purge(world: World):
    out = world.tmp / "env.txt"
    hook = {
        "type": "command",
        "blocking": True,
        "run": (
            'test -d "$CCW_STATE_DIR" && '
            f'echo "$CCW_STATE_DIR|$CCW_API_URL|$CCW_CLAUDE_CONFIG_DIR" >> {out}'
        ),
    }
    cfg = world.cfg(hooks={"onCreate": [hook]})
    m = world.manager(cfg, api_url="http://127.0.0.1:4242")
    snap = await m.create(CreateSessionRequest())
    sid = snap["session_id"]
    state, api, cfg_dir = out.read_text().strip().split("|")
    sdir = world.session_dir(sid)
    assert state == str(sdir / "hook-state")
    assert api == "http://127.0.0.1:4242"
    assert cfg_dir == str(m._active[sid].config_dir)
    assert cfg_dir == m._child_env(m._active[sid])["CLAUDE_CONFIG_DIR"]
    (sdir / "hook-state" / "keep").write_text("x")
    await m.destroy(sid)
    assert (sdir / "hook-state" / "keep").is_file()
    await m.destroy(sid, purge=True)
    assert not sdir.exists()
