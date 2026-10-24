"""claude.main_agent_only_tools: PreToolUse denies matching tools for subagents (hive D2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_router import Harness

from claude_code_wrapper.config import Config

TOOL = "mcp__any-mcp__orchestrator_handover"
README = Path(__file__).resolve().parent.parent / "README.md"


def make(tmp_path, globs):
    h = Harness(tmp_path)
    h.router._cfg = h.cfg.model_copy(
        update={"claude": h.cfg.claude.model_copy(update={"main_agent_only_tools": globs})}
    )
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
    (tmp_path / "b").mkdir()
    h2 = make(tmp_path / "b", ["mcp__any-mcp__orchestrator_*"])
    r = await call(h2, "Bash", agent_id="ag1")
    assert r["stdout"] == "" and not h2.events("tool_denied")
    await h.router.close()
    await h2.router.close()


def test_key_validates_as_list_of_strings():
    with pytest.raises(ValidationError):
        Config.model_validate({"claude": {"main_agent_only_tools": "mcp__x__*"}})


def test_readme_documents_best_effort():
    text = README.read_text()
    trust = text.split("## Trust model", 1)[1].split("\n## ", 1)[0]
    assert "main_agent_only_tools" in trust
    assert "best-effort" in trust and "not a security boundary" in trust
