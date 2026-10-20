"""Prometheus metrics (D18): the Metrics registry, router/manager instrumentation, /metrics."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_manager import CreateSessionRequest, World, deny_hook, world  # noqa: F401
from test_router import (
    CLAUDE_SID,
    SID,
    Harness,
    assistant,
    completed_record,
    webhook,
)

from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.config import Config
from claude_code_wrapper.hooks import ActionResult
from claude_code_wrapper.manager import CreateFailed, CreateVetoed
from claude_code_wrapper.metrics import MAX_TOOL_LABELS, Metrics
from claude_code_wrapper.redact import Redactor
from claude_code_wrapper.router import SessionInfo, SessionRouter
from claude_code_wrapper.turns import TurnRecord, now_iso


def sample(m: Metrics, name: str, **labels: str) -> float:
    value = m.registry.get_sample_value(name, labels)
    return 0.0 if value is None else value


def with_metrics(h: Harness, m: Metrics) -> Harness:
    h.dispatcher._on_result = None
    h.router = SessionRouter(
        h.session,  # type: ignore[arg-type]
        h.log,
        h.dispatcher,
        h.redactor,
        h.cfg,
        session_info=SessionInfo(SID, CLAUDE_SID, str(h.workdir), "http://session.cb/x"),
        metrics=m,
    )
    return h


def test_instances_are_isolated() -> None:
    a, b = Metrics(), Metrics()
    a.sessions_created("ok")
    assert sample(a, "ccw_sessions_created_total", result="ok") == 1
    assert sample(b, "ccw_sessions_created_total", result="ok") == 0


def test_tool_label_clamped_after_fifty_names() -> None:
    m = Metrics()
    for i in range(MAX_TOOL_LABELS):
        m.tool_call(f"tool{i}", "allow")
    m.tool_call("tool0", "allow")
    m.tool_call("brand-new", "deny")
    assert MAX_TOOL_LABELS == 50
    assert sample(m, "ccw_tool_calls_total", tool="tool0", decision="allow") == 2
    assert sample(m, "ccw_tool_calls_total", tool="brand-new", decision="deny") == 0
    assert sample(m, "ccw_tool_calls_total", tool="other", decision="deny") == 1


def test_turn_and_token_helpers() -> None:
    m = Metrics()
    record = TurnRecord(
        turn_id="t",
        session_id=SID,
        state="completed",
        started_at="2026-01-01T00:00:00.000+00:00",
        completed_at="2026-01-01T00:00:02.500+00:00",
    )
    m.turn_finished(record)
    assert sample(m, "ccw_turns_total", origin="client", state="completed") == 1
    assert sample(m, "ccw_turn_duration_seconds_sum") == pytest.approx(2.5)
    m.tokens(
        {
            "m1": {
                "input_tokens": 1,
                "output_tokens": 2,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 4,
            }
        }
    )
    for kind, n in (("input", 1), ("output", 2), ("cache_read", 3), ("cache_creation", 4)):
        assert sample(m, "ccw_turn_tokens_total", model="m1", kind=kind) == n
    m.claude_restart()
    assert sample(m, "ccw_claude_restarts_total") == 1


def test_metrics_route_unauthenticated_text_format() -> None:
    m = Metrics()
    m.sessions_created("ok")

    class Stub:
        started = True
        redactor = Redactor()

    cfg = Config.model_validate({"server": {"auth_token": "tok-123456789"}})
    client = TestClient(create_app(Stub(), cfg, metrics=m))
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert 'ccw_sessions_created_total{result="ok"} 1.0' in r.text
    assert client.get("/v1/sessions").status_code == 401


def test_metrics_route_without_metrics_is_empty() -> None:
    class Stub:
        started = True
        redactor = Redactor()

    client = TestClient(create_app(Stub(), Config.model_validate({})))
    r = client.get("/metrics")
    assert r.status_code == 200 and r.text == ""


async def test_router_tool_and_hook_metrics(tmp_path: Path) -> None:
    m = Metrics()
    h = with_metrics(Harness(tmp_path, hooks={"onAgentToolCall": [webhook()]}), m)
    await h.router.handle_claude_hook("PreToolUse", {"tool_name": "Read", "tool_input": {}})
    h.reply = {"decision": "deny", "reason": "no"}
    await h.router.handle_claude_hook("PreToolUse", {"tool_name": "Bash", "tool_input": {}})
    assert sample(m, "ccw_tool_calls_total", tool="Read", decision="deny") == 0
    assert sample(m, "ccw_tool_calls_total", tool="Bash", decision="deny") == 1
    assert (
        sample(m, "ccw_hook_actions_total", event="onAgentToolCall", type="webhook", result="ok")
        == 2
    )
    h.router.on_hook_result(ActionResult("onTurn", 0, "command", ok=False, error="boom"))
    assert sample(m, "ccw_hook_actions_total", event="onTurn", type="command", result="error") == 1
    h.router.on_restart(1, True)
    assert sample(m, "ccw_claude_restarts_total") == 1
    await h.router.close()


async def test_router_stop_records_turn_and_tokens(tmp_path: Path) -> None:
    m = Metrics()
    h = with_metrics(Harness(tmp_path), m)
    h.transcript.write_text(assistant("done", usage={"input_tokens": 7, "output_tokens": 2}))
    h.session.complete_result = completed_record(completed_at=now_iso())
    await h.router.handle_claude_hook("Stop", {"session_id": CLAUDE_SID})
    assert sample(m, "ccw_turns_total", origin="client", state="completed") == 1
    assert sample(m, "ccw_turn_tokens_total", model="claude-x", kind="input") == 7
    assert sample(m, "ccw_turn_tokens_total", model="claude-x", kind="output") == 2
    assert sample(m, "ccw_turn_duration_seconds_count") == 1
    failed = completed_record(state="failed", completed_at=now_iso())
    h.router.on_turn_end(failed)
    await h.router.drain()
    assert sample(m, "ccw_turns_total", origin="client", state="failed") == 1
    await h.router.close()


async def test_manager_session_metrics(world: World) -> None:  # noqa: F811
    m = Metrics()
    mgr = world.manager(world.cfg(), metrics=m)
    snap = await mgr.create(CreateSessionRequest())
    assert sample(m, "ccw_sessions_active") == 1
    assert sample(m, "ccw_sessions_created_total", result="ok") == 1
    await mgr.destroy(snap["session_id"])
    assert sample(m, "ccw_sessions_active") == 0

    veto = world.manager(world.cfg(hooks={"onCreate": [deny_hook("no")]}), metrics=m)
    with pytest.raises(CreateVetoed):
        await veto.create(CreateSessionRequest())
    assert sample(m, "ccw_sessions_created_total", result="vetoed") == 1
    assert sample(m, "ccw_sessions_active") == 0

    bad = world.manager(world.cfg(init={"setup_commands": ["exit 3"]}), metrics=m)
    with pytest.raises(CreateFailed):
        await bad.create(CreateSessionRequest())
    assert sample(m, "ccw_sessions_created_total", result="failed") == 1
    assert sample(m, "ccw_sessions_active") == 0
