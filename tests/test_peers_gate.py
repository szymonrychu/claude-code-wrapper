"""PreToolUse gate for Claude's native peer messaging when peers are enabled."""

import json

import pytest
from test_router import Harness, webhook

from claude_code_wrapper.config import Config
from claude_code_wrapper.router import CCW_SUBAGENT_DENY_REASON

GATE_REASON = "use mcp__ccw__send_message / mcp__ccw__peers"
PEERS = {"enabled": True, "token": "peer-token-1234567890"}


def _enable(h):
    h.cfg = Config.model_validate({**h.cfg.model_dump(), "peers": PEERS})
    h.router._cfg = h.cfg


async def _pre(h, tool, **extra):
    return await h.router.handle_claude_hook(
        "PreToolUse", {"tool_use_id": "tu1", "tool_name": tool, "tool_input": {}, **extra}
    )


@pytest.mark.parametrize("tool", ["SendMessage", "ListAgents"])
async def test_native_peer_tools_denied_when_enabled(tmp_path, tool):
    h = Harness(tmp_path, hooks={"onAgentToolCall": [webhook()]})
    _enable(h)
    out = json.loads((await _pre(h, tool))["stdout"])["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == GATE_REASON
    (ev,) = h.events("tool_denied")
    assert ev.data["tool_name"] == tool and ev.data["reason"] == GATE_REASON
    assert h.hook_posts == [] and not h.events("tool_call")
    await h.router.close()


async def test_native_peer_tool_denial_carries_attribution(tmp_path):
    h = Harness(tmp_path)
    _enable(h)
    await _pre(h, "SendMessage", agent_id="ag1", agent_type="Explore")
    (ev,) = h.events("tool_denied")
    assert ev.data["agent_id"] == "ag1" and ev.data["agent_type"] == "Explore"
    await h.router.close()


@pytest.mark.parametrize("tool", ["SendMessage", "ListAgents"])
async def test_native_peer_tools_allowed_when_disabled(tmp_path, tool):
    h = Harness(tmp_path, hooks={"onAgentToolCall": [webhook()]})
    r = await _pre(h, tool)
    assert r["stdout"] == ""
    assert h.events("tool_call") and not h.events("tool_denied")
    assert len(h.hook_posts) == 1
    await h.router.close()


@pytest.mark.parametrize("tool", ["mcp__ccw__send_message", "mcp__ccw__peers"])
async def test_subagent_ccw_peer_tool_denied_with_reworded_reason(tmp_path, tool):
    h = Harness(tmp_path)
    _enable(h)
    r = await _pre(h, tool, agent_id="ag1", agent_type="Explore")
    out = json.loads(r["stdout"])["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == CCW_SUBAGENT_DENY_REASON
    assert "main agent only" in CCW_SUBAGENT_DENY_REASON
    assert "mcp__ccw__*" in CCW_SUBAGENT_DENY_REASON
    await h.router.close()
