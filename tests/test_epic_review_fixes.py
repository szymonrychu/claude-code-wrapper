"""Epic review remediation: fail-closed gates, onStop on_error, state dir mode, HMAC secret."""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import time

from conftest import World
from test_example_tatara import SCRIPT, run_script
from test_router import Harness, webhook

from claude_code_wrapper.config import HooksConfig
from claude_code_wrapper.hooks import HookDispatcher, build_payload, gate_fail_mode
from claude_code_wrapper.manager import CreateSessionRequest
from claude_code_wrapper.redact import Redactor

PRE = {"tool_name": "Bash", "tool_use_id": "tu1", "tool_input": {"command": "ls"}}


def test_gate_fail_mode_none_considers_every_gate():
    hooks = HooksConfig.model_validate(
        {"onAgentToolCall": [{**webhook(on_error="deny"), "matcher": "Edit"}]}
    )
    assert gate_fail_mode(hooks, "Bash") == "allow"
    assert gate_fail_mode(hooks, None) == "deny"


async def test_pretooluse_empty_tool_name_fails_closed(tmp_path):
    h = Harness(
        tmp_path, hooks={"onAgentToolCall": [{**webhook(on_error="deny"), "matcher": "Edit"}]}
    )

    async def boom(*a, **k):
        raise RuntimeError("down")

    h.dispatcher.fire = boom  # type: ignore[method-assign]
    for payload in ({"tool_use_id": "t"}, {"tool_name": "", "tool_use_id": "t"}):
        res = await h.router.handle_claude_hook("PreToolUse", payload)
        assert "deny" in res["stdout"]
    await h.router.close()


async def test_failed_blocking_onstop_with_deny_blocks(tmp_path):
    action = {"type": "command", "run": "exit 1", "blocking": True, "on_error": "deny"}
    d = HookDispatcher(
        HooksConfig.model_validate({"onStop": [action]}),
        Redactor(),
        base_env={"PATH": os.environ["PATH"]},
        default_cwd=tmp_path,
    )
    payload = build_payload(
        "onStop", session_id="s", claude_session_id="c", turn_id="t", workdir="/w", data={}
    )
    o = await d.fire("onStop", payload)
    assert o.decision == "block"
    assert o.reason and "failed" in o.reason


async def test_relay_deny_returned_before_slow_on_error(tmp_path):
    h = Harness(
        tmp_path,
        hooks={"onAgentToolCall": [webhook(on_error="deny")], "onError": [webhook()]},
    )
    real = h.dispatcher.fire

    async def fire(event, *a, **k):
        if event == "onError":
            await asyncio.sleep(3)
            return await real(event, *a, **k)
        raise RuntimeError("down")

    h.dispatcher.fire = fire  # type: ignore[method-assign]
    t0 = time.monotonic()
    res = await h.router.handle_claude_hook("PreToolUse", PRE)
    assert time.monotonic() - t0 < 1.5
    assert "deny" in res["stdout"]
    await h.router.close()


async def test_hook_state_dir_is_private(world: World):
    m = world.manager(world.cfg({"turns": []}))
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    m.router(sid)
    state = world.session_dir(sid) / "hook-state"
    assert stat.S_IMODE(state.stat().st_mode) == 0o700


def test_callback_refuses_empty_hmac_secret(tmp_path):
    state = tmp_path / "st"
    state.mkdir()
    run = run_script(
        "finalize",
        {"turn_id": "t1", "data": {"state": "completed"}, "workdir": str(tmp_path)},
        state,
        DEFAULT_CALLBACK_URL="http://127.0.0.1:9/cb",
        CALLBACK_HMAC_SECRET="",
    )
    assert run.returncode != 0
    assert "CALLBACK_HMAC_SECRET" in run.stderr
    assert str(SCRIPT) and sys.executable
