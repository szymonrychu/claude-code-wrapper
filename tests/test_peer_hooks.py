"""Instance-level membership hooks: preJoin, postJoin, preLeave, postLeave."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from claude_code_wrapper.config import HOOK_EVENTS, VETO_EVENTS, Config, HooksConfig
from claude_code_wrapper.hooks import (
    EVENT_DECISIONS,
    INSTANCE_EVENTS,
    fire_instance,
    instance_dispatcher,
)
from claude_code_wrapper.redact import Redactor

DATA = {
    "instance": "a",
    "url": "http://a:8080",
    "members": [{"instance": "b", "url": "http://b:8080"}],
}


def make(tmp_path: Path, hooks: dict):
    cfg = Config.model_validate(
        {"paths": {"sessions_root": str(tmp_path / "sessions")}, "hooks": hooks}
    )
    return instance_dispatcher(
        cfg, Redactor([]), service_env={"PATH": os.environ["PATH"]}, api_url="http://127.0.0.1:1"
    )


def cmd(run, **kw):
    return {"type": "command", "run": run, **kw}


def test_instance_events_constant():
    assert INSTANCE_EVENTS == ("preJoin", "postJoin", "preLeave", "postLeave")
    assert HOOK_EVENTS[-4:] == INSTANCE_EVENTS
    assert "preJoin" in VETO_EVENTS


@pytest.mark.parametrize("event", INSTANCE_EVENTS)
def test_hook_config_lists(event):
    cfg = HooksConfig.model_validate({event: [cmd("true")]})
    assert len(cfg.for_event(event)) == 1


@pytest.mark.parametrize("event", INSTANCE_EVENTS)
@pytest.mark.parametrize("key,val", [("interval_seconds", 5), ("when", "idle")])
def test_hook_config_lists_reject_tick_keys(event, key, val):
    with pytest.raises(ValidationError):
        HooksConfig.model_validate({event: [cmd("true", **{key: val})]})


def test_event_decisions():
    assert EVENT_DECISIONS["preJoin"] == ("allow", "deny")
    for event in ("postJoin", "preLeave", "postLeave"):
        assert EVENT_DECISIONS[event] == ("allow",)


async def test_fire_instance_payload(tmp_path):
    out = tmp_path / "out.json"
    d = make(tmp_path, {"preJoin": [cmd(f"cat > {out}; pwd > {tmp_path}/cwd", blocking=True)]})
    assert (tmp_path / "sessions" / ".peers-hook-state").is_dir()
    assert (tmp_path / "sessions" / ".peers-hook-state").stat().st_mode & 0o777 == 0o700
    outcome = await fire_instance(d, "preJoin", DATA)
    assert outcome.decision == "allow"
    p = json.loads(out.read_text())
    assert p["event"] == "preJoin"
    assert p["session_id"] == ""
    assert p["claude_session_id"] is None
    assert p["turn_id"] is None
    assert p["workdir"] == ""
    assert p["data"] == DATA
    assert (tmp_path / "cwd").read_text().strip() == str((tmp_path / "sessions").resolve())


async def test_veto_and_on_error(tmp_path):
    d = make(
        tmp_path,
        {
            "preJoin": [cmd("exit 2", blocking=True)],
            "postJoin": [cmd("exit 2", blocking=True)],
        },
    )
    assert (await fire_instance(d, "preJoin", DATA)).decision == "deny"
    assert (await fire_instance(d, "postJoin", DATA)).decision == "allow"


async def test_failing_prejoin_on_error_policy(tmp_path):
    d = make(tmp_path, {"preJoin": [cmd("exit 1", blocking=True, on_error="deny")]})
    assert (await fire_instance(d, "preJoin", DATA)).decision == "deny"
    d = make(tmp_path, {"preJoin": [cmd("exit 1", blocking=True)]})
    assert (await fire_instance(d, "preJoin", DATA)).decision == "allow"


async def test_fire_instance_never_raises(tmp_path):
    class Boom:
        async def fire(self, *a, **k):
            raise RuntimeError("x")

    assert (await fire_instance(Boom(), "postJoin", DATA)).decision == "allow"  # type: ignore[arg-type]
    assert (await fire_instance(Boom(), "preJoin", DATA)).decision == "allow"  # type: ignore[arg-type]


async def test_non_blocking_postleave_returns_at_once(tmp_path):
    d = make(tmp_path, {"postLeave": [cmd("sleep 3")]})
    t = time.monotonic()
    outcome = await fire_instance(d, "postLeave", DATA)
    assert time.monotonic() - t < 1.5
    assert outcome.decision == "allow"
    await d.drain(0)


async def test_veto_and_on_error_combined(tmp_path):
    await test_veto_and_on_error(tmp_path)
    await test_failing_prejoin_on_error_policy(tmp_path)
