"""DEF-1: a ccw-side failure denies a PreToolUse call when a matching gate has on_error: deny."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from test_router import Harness, webhook

from claude_code_wrapper.config import Config
from claude_code_wrapper.hooks import gate_fail_mode

PRE = {"tool_use_id": "tu1", "tool_name": "Bash", "tool_input": {}}


def hooks_cfg(*actions):
    return Config.model_validate({"hooks": {"onAgentToolCall": list(actions)}}).hooks


def test_gate_fail_mode_matcher_aware():
    cfg = hooks_cfg(webhook(on_error="deny", matcher="Bash"))
    assert gate_fail_mode(cfg, "Bash") == "deny"
    assert gate_fail_mode(cfg, "Read") == "allow"
    assert gate_fail_mode(cfg) == "deny"
    assert gate_fail_mode(hooks_cfg(webhook(on_error="deny", matcher="Bash")), "") == "allow"
    assert gate_fail_mode(hooks_cfg(webhook(on_error="deny")), "Read") == "deny"
    assert gate_fail_mode(hooks_cfg(webhook(on_error="deny", blocking=False)), "Bash") == "allow"
    assert gate_fail_mode(hooks_cfg(webhook()), "Bash") == "allow"
    assert gate_fail_mode(hooks_cfg(), None) == "allow"


async def test_dispatcher_raises_on_unusable_event(tmp_path):
    h = Harness(tmp_path)
    with pytest.raises(Exception):  # noqa: B017
        await h.dispatcher.fire("onNoSuchEvent", {})
    await h.router.close()


async def test_dispatcher_raises_when_redaction_fails(tmp_path):
    h = Harness(tmp_path, hooks={"onAgentToolCall": [webhook()]})

    def boom(_):
        raise RuntimeError("redact")

    real = h.redactor.redact_obj
    h.redactor.redact_obj = boom  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError):
            await h.dispatcher.fire("onAgentToolCall", {}, tool_name="Bash")
    finally:
        h.redactor.redact_obj = real  # type: ignore[method-assign]
    await h.router.close()


async def _break_fire(h):
    async def boom(*a, **k):
        raise RuntimeError("dispatcher down")

    h.dispatcher.fire = boom  # type: ignore[method-assign]


async def test_failure_denies_with_matching_gate(tmp_path):
    h = Harness(tmp_path, hooks={"onAgentToolCall": [webhook(on_error="deny")]})
    seen = []
    h.router._metrics = SimpleNamespace(tool_call=lambda t, d: seen.append((t, d)))
    await _break_fire(h)
    r = await h.router.handle_claude_hook("PreToolUse", PRE)
    out = json.loads(r["stdout"])["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == "ccw hook relay failed: RuntimeError"
    assert h.events("error")[0].data["kind"] == "hook_relay"
    assert h.events("tool_denied")
    assert seen == [("Bash", "deny")]
    await h.router.close()


async def test_failing_emit_cannot_turn_deny_into_allow(tmp_path):
    h = Harness(tmp_path, hooks={"onAgentToolCall": [webhook(on_error="deny")]})
    await _break_fire(h)
    real = h.router._emit

    def emit(type, data=None):
        if type in ("error", "tool_denied"):
            raise OSError("disk")
        return real(type, data)

    h.router._emit = emit  # type: ignore[method-assign]
    r = await h.router.handle_claude_hook("PreToolUse", PRE)
    assert json.loads(r["stdout"])["hookSpecificOutput"]["permissionDecision"] == "deny"
    await h.router.close()


async def test_non_matching_or_no_gate_allows(tmp_path):
    for i in range(3):
        (tmp_path / f"h{i}").mkdir()
    cases = (
        {"onAgentToolCall": [webhook(on_error="deny", matcher="Read")]},
        {"onAgentToolCall": [webhook()]},
        {},
    )
    for i, hooks in enumerate(cases):
        h = Harness(tmp_path / f"h{i}", hooks=hooks)
        await _break_fire(h)
        r = await h.router.handle_claude_hook("PreToolUse", PRE)
        assert r["stdout"] == "" and r["exit_code"] == 0
        assert h.events("error")
        await h.router.close()


async def test_other_events_still_swallow(tmp_path):
    h = Harness(tmp_path, hooks={"onToolResult": [webhook()]})
    await _break_fire(h)
    r = await h.router.handle_claude_hook(
        "PostToolUse", {"tool_use_id": "t", "tool_name": "Bash", "tool_response": {}}
    )
    assert r["stdout"] == ""
    await h.router.close()
