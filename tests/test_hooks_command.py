"""Command actions, matching, veto semantics and the envelope."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from claude_code_wrapper.config import HooksConfig
from claude_code_wrapper.hooks import HookDispatcher, build_payload
from claude_code_wrapper.redact import Redactor


def make(tmp_path: Path, hooks: dict, **kw) -> tuple[HookDispatcher, list]:
    results: list = []
    d = HookDispatcher(
        HooksConfig.model_validate(hooks),
        Redactor(["supersecretvalue"]),
        base_env={"PATH": os.environ["PATH"], "BASE": "b"},
        default_cwd=tmp_path,
        on_result=results.append,
        **kw,
    )
    return d, results


def payload(event="onTurn", **data):
    return build_payload(
        event,
        session_id="s1",
        claude_session_id="c1",
        turn_id="t1",
        workdir="/w",
        data=data,
    )


def cmd(run, **kw):
    return {"type": "command", "run": run, **kw}


def test_build_payload_envelope():
    p = payload("onTurn", text="hi")
    assert set(p) == {
        "event",
        "session_id",
        "claude_session_id",
        "turn_id",
        "ts",
        "workdir",
        "data",
    }
    assert p["ts"].endswith("Z") or p["ts"].endswith("+00:00")
    assert p["data"] == {"text": "hi"}


async def test_command_receives_payload_on_stdin_and_env(tmp_path):
    out = tmp_path / "out.txt"
    d, results = make(
        tmp_path,
        {
            "onTurn": [
                cmd(
                    f"cat > {out}; echo $CCW_EVENT $CCW_SESSION_ID $CCW_TURN_ID $CCW_WORKDIR "
                    f"$CCW_CLAUDE_CONFIG_DIR $BASE $X >> {out}; pwd >> {out}",
                    env={"X": "x"},
                    blocking=True,
                )
            ]
        },
        config_dir="/cfg",
    )
    o = await d.fire("onTurn", payload(text="supersecretvalue"))
    assert o.decision == "allow" and results[0].ok
    lines = out.read_text().splitlines()
    body, env_line, pwd = lines[0], lines[0].split("}")[-1], lines[-1]
    assert "supersecretvalue" not in out.read_text() and "***" in out.read_text()
    assert env_line.strip() == "onTurn s1 t1 /w /cfg b x"
    assert Path(pwd).resolve() == tmp_path.resolve()
    assert body


async def test_cwd_override(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    d, _ = make(tmp_path, {"onTurn": [cmd("pwd > here", blocking=True)]})
    await d.fire("onTurn", payload(), cwd=sub)
    assert (sub / "here").exists()


async def test_exit_2_denies_with_stderr_reason(tmp_path):
    d, results = make(
        tmp_path,
        {
            "onTurn": [
                cmd("echo nope >&2; exit 2", blocking=True),
                cmd("echo later >&2; exit 2", blocking=True),
            ]
        },
    )
    o = await d.fire("onTurn", payload())
    assert o.decision == "deny" and o.reason == "nope"
    assert [r.decision for r in o.results] == ["deny", "deny"]
    assert len(results) == 2


async def test_stderr_capped(tmp_path):
    d, _ = make(
        tmp_path,
        {"onTurn": [cmd("head -c 5000 /dev/zero | tr '\\0' a >&2; exit 2", blocking=True)]},
    )
    o = await d.fire("onTurn", payload())
    assert len(o.reason) == 2000


async def test_stdout_json_decision(tmp_path):
    d, _ = make(
        tmp_path,
        {
            "onTurn": [
                cmd("""echo '{"additional_context":"A"}'""", blocking=True),
                cmd(
                    """echo '{"decision":"deny","reason":"r","additional_context":"B"}'""",
                    blocking=True,
                ),
                cmd("echo not json", blocking=True),
            ]
        },
    )
    o = await d.fire("onTurn", payload())
    assert o.decision == "deny" and o.reason == "r"
    assert o.additional_context == "A\n\nB"


async def test_deny_ignored_for_non_veto_events(tmp_path):
    d, _ = make(tmp_path, {"onTurnComplete": [cmd("exit 2", blocking=True)]})
    o = await d.fire("onTurnComplete", payload("onTurnComplete"))
    assert o.decision == "allow" and o.results[0].decision == "deny"


async def test_error_exit_on_error_policy(tmp_path):
    d, _ = make(tmp_path, {"onTurn": [cmd("exit 1", blocking=True, on_error="deny")]})
    o = await d.fire("onTurn", payload())
    assert o.decision == "deny" and not o.results[0].ok and o.results[0].error
    d, _ = make(tmp_path, {"onTurn": [cmd("exit 1", blocking=True)]})
    o = await d.fire("onTurn", payload())
    assert o.decision == "allow" and not o.results[0].ok


async def test_timeout_kills_and_applies_on_error(tmp_path):
    marker = tmp_path / "survived"
    d, _ = make(
        tmp_path,
        {
            "onTurn": [
                cmd(
                    f"(sleep 1.5; touch {marker}) & sleep 30",
                    blocking=True,
                    timeout_seconds=0.5,
                    on_error="deny",
                )
            ]
        },
    )
    t0 = time.monotonic()
    o = await d.fire("onTurn", payload())
    assert time.monotonic() - t0 < 5
    assert o.decision == "deny" and "timeout" in o.results[0].error
    time.sleep(2)
    assert not marker.exists()


async def test_matcher_filters_tool_name(tmp_path):
    d, _ = make(
        tmp_path,
        {
            "onAgentToolCall": [
                cmd("exit 2", blocking=True, matcher="Bash|Edit"),
                cmd('echo \'{"additional_context":"any"}\'', blocking=True),
            ]
        },
    )
    o = await d.fire("onAgentToolCall", payload("onAgentToolCall"), tool_name="Bash")
    assert o.decision == "deny" and len(o.results) == 2
    o = await d.fire("onAgentToolCall", payload("onAgentToolCall"), tool_name="BashX")
    assert o.decision == "allow" and len(o.results) == 1
    assert o.results[0].index == 1


async def test_blocking_run_concurrently(tmp_path):
    d, _ = make(
        tmp_path, {"onTurn": [cmd("sleep 1", blocking=True), cmd("sleep 1", blocking=True)]}
    )
    t0 = time.monotonic()
    await d.fire("onTurn", payload())
    assert time.monotonic() - t0 < 1.9


async def test_non_blocking_does_not_delay(tmp_path):
    marker = tmp_path / "done"
    d, results = make(tmp_path, {"onTurn": [cmd(f"sleep 0.5; touch {marker}")]})
    t0 = time.monotonic()
    o = await d.fire("onTurn", payload())
    assert time.monotonic() - t0 < 0.3 and o.results == []
    await d.drain(5)
    assert marker.exists() and len(results) == 1


async def test_drain_cancels_after_timeout(tmp_path):
    d, _ = make(tmp_path, {"onTurn": [cmd("sleep 30")]})
    await d.fire("onTurn", payload())
    t0 = time.monotonic()
    await d.drain(0.2)
    assert time.monotonic() - t0 < 3


async def test_never_raises_on_bad_cwd(tmp_path):
    d, _ = make(tmp_path, {"onTurn": [cmd("true", blocking=True, on_error="deny")]})
    o = await d.fire("onTurn", payload(), cwd=tmp_path / "missing")
    assert o.decision == "deny" and not o.results[0].ok


async def test_no_hooks_allows(tmp_path):
    d, _ = make(tmp_path, {})
    o = await d.fire("onTurn", payload())
    assert o.decision == "allow" and o.results == [] and o.additional_context == ""


def test_json_payload_roundtrip():
    assert json.loads(json.dumps(payload()))["event"] == "onTurn"


async def test_deny_wins_across_actions(tmp_path):
    d, _ = make(
        tmp_path,
        {
            "onTurn": [
                cmd("exit 0", blocking=True),
                cmd("echo blocked >&2; exit 2", blocking=True),
                cmd('echo \'{"decision":"allow"}\'', blocking=True),
            ]
        },
    )
    o = await d.fire("onTurn", payload())
    assert o.decision == "deny" and o.reason == "blocked"
    assert [r.decision for r in o.results] == ["allow", "deny", "allow"]


async def test_drain_waits_pending(tmp_path):
    marker = tmp_path / "late"
    d, results = make(tmp_path, {"onTurn": [cmd(f"sleep 0.4; touch {marker}")]})
    await d.fire("onTurn", payload())
    assert not marker.exists() and results == []
    await d.drain(5)
    assert marker.exists() and len(results) == 1 and results[0].ok


async def test_config_dir_env(tmp_path):
    out = tmp_path / "cfg.txt"
    d, _ = make(
        tmp_path,
        {"onTurn": [cmd(f"echo $CCW_CLAUDE_CONFIG_DIR > {out}", blocking=True)]},
        config_dir="/some/claude/home",
    )
    await d.fire("onTurn", payload())
    assert out.read_text().strip() == "/some/claude/home"
