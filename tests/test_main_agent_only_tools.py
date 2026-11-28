"""claude.main_agent_only_tools: PreToolUse denies matching tools for subagents (hive D2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_router import Harness

from claude_code_wrapper.config import Config
from claude_code_wrapper.router import (
    CCW_SUBAGENT_DENY_REASON,
    CCW_TOOL_PREFIX,
    MAIN_AGENT_ONLY_DENY_REASON,
)

TOOL = "mcp__any-mcp__orchestrator_handover"
README = Path(__file__).resolve().parent.parent / "README.md"


def make(tmp_path, globs, allowed=None):
    h = Harness(tmp_path)
    upd = {"main_agent_only_tools": globs}
    if allowed is not None:
        upd["subagent_allowed_tools"] = allowed
    h.router._cfg = h.cfg.model_copy(update={"claude": h.cfg.claude.model_copy(update=upd)})
    return h


def call(h, tool, **extra):
    return h.router.handle_claude_hook(
        "PreToolUse", {"tool_use_id": "t1", "tool_name": tool, "tool_input": {}, **extra}
    )


async def test_subagent_call_denied(tmp_path):
    h = make(tmp_path, ["mcp__any-mcp__orchestrator_*"])
    r = await call(h, TOOL, agent_id="ag1", agent_type="Explore")
    out = json.loads(r["stdout"])["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "main agent only" in out["permissionDecisionReason"]
    (ev,) = h.events("tool_denied")
    assert ev.data["agent_id"] == "ag1" and ev.data["tool_name"] == TOOL
    assert not h.events("tool_call")
    await h.router.close()


async def test_main_agent_allowed(tmp_path):
    h = make(tmp_path, ["mcp__any-mcp__orchestrator_*"])
    r = await call(h, TOOL)
    assert r["stdout"] == ""
    assert h.events("tool_call") and not h.events("tool_denied")
    await h.router.close()


async def test_default_empty_changes_nothing(tmp_path):
    assert Config().claude.main_agent_only_tools == []
    h = Harness(tmp_path)
    r = await call(h, TOOL, agent_id="ag1")
    assert r["stdout"] == "" and not h.events("tool_denied")
    (tmp_path / "b").mkdir()
    h2 = make(tmp_path / "b", ["mcp__any-mcp__orchestrator_*"])
    r = await call(h2, "Bash", agent_id="ag1")
    assert r["stdout"] == "" and not h2.events("tool_denied")
    await h.router.close()
    await h2.router.close()


def test_key_validates_as_list_of_strings():
    with pytest.raises(ValidationError):
        Config.model_validate({"claude": {"main_agent_only_tools": "mcp__x__*"}})


MAIN_ONLY = ["mcp__any-mcp__orchestrator_*"]
HISTORY = "mcp__any-mcp__orchestrator_history_get"
PATCH = "mcp__any-mcp__orchestrator_session_patch"


async def test_subagent_allowed_tool_passes(tmp_path):
    h = make(tmp_path, MAIN_ONLY, [HISTORY])
    r = await call(h, HISTORY, agent_id="ag1")
    assert r["stdout"] == "" and not h.events("tool_denied")
    await h.router.close()


async def test_subagent_other_tool_still_denied(tmp_path):
    h = make(tmp_path, MAIN_ONLY, [HISTORY])
    r = await call(h, PATCH, agent_id="ag1")
    out = json.loads(r["stdout"])["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == MAIN_AGENT_ONLY_DENY_REASON
    await h.router.close()


async def test_main_agent_unaffected(tmp_path):
    h = make(tmp_path, MAIN_ONLY, [HISTORY])
    for tool in (HISTORY, PATCH):
        r = await call(h, tool)
        assert r["stdout"] == ""
    assert not h.events("tool_denied")
    await h.router.close()


async def test_allowed_list_does_not_open_ccw_tools(tmp_path):
    ccw = CCW_TOOL_PREFIX + "command"
    h = make(tmp_path, MAIN_ONLY, [ccw])
    r = await call(h, ccw, agent_id="ag1")
    out = json.loads(r["stdout"])["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == CCW_SUBAGENT_DENY_REASON
    await h.router.close()
