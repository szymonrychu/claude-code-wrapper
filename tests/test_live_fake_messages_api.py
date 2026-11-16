"""The live job's fake Messages API scripts replies the way real Claude Code shapes requests."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

FAKE = Path(__file__).parent / "live" / "fake_messages_api.py"
TOOL = {"name": "Bash", "input": {"command": "echo hi", "description": "x"}}
PROMPT = f"LIVE-CASE:case-1 LIVE-TOOL:{json.dumps(TOOL, separators=(',', ':'))}"
OFFERED = [{"name": "Bash"}]


def _fake() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fake_messages_api_mod", FAKE)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tool_use() -> dict[str, object]:
    return {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": TOOL["input"]}],
    }


def _tool_result() -> dict[str, object]:
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "hi"}],
    }


def test_first_request_answers_with_the_tool() -> None:
    body = {"tools": OFFERED, "messages": [{"role": "user", "content": PROMPT}]}
    case, kind, value, results = _fake().decide(body)
    assert (case, kind, value, results) == ("case-1", "tool", TOOL, [])


def test_tool_result_in_last_message_ends_the_case() -> None:
    body = {
        "tools": OFFERED,
        "messages": [{"role": "user", "content": PROMPT}, _tool_use(), _tool_result()],
    }
    case, kind, value, results = _fake().decide(body)
    assert (case, kind, value) == ("case-1", "text", "done case-1")
    assert [r["tool_use_id"] for r in results] == ["toolu_1"]


def test_hook_context_after_the_tool_result_still_ends_the_case() -> None:
    """A PostToolBatch/PostToolUse additionalContext arrives as a user message after the
    tool_result one; the case is still past its tool call and must not call it again."""
    reminder = {
        "role": "user",
        "content": [
            {"type": "text", "text": "<system-reminder>LIVE-PRESSURE-l1</system-reminder>"}
        ],
    }
    body = {
        "tools": OFFERED,
        "messages": [{"role": "user", "content": PROMPT}, _tool_use(), _tool_result(), reminder],
    }
    case, kind, value, results = _fake().decide(body)
    assert (case, kind, value) == ("case-1", "text", "done case-1")
    assert [r["tool_use_id"] for r in results] == ["toolu_1"]
