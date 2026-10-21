"""K5 agent attribution: agent_id/agent_type on tool events and per-message usage events."""

from __future__ import annotations

import json
from typing import Any

from conftest import World, wait_turn

from claude_code_wrapper.events import EVENT_TYPES
from claude_code_wrapper.manager import CreateSessionRequest


def say(obj: dict[str, Any]) -> dict[str, Any]:
    return {"type": "command", "run": f"printf '%s' '{json.dumps(obj)}'", "blocking": True}


def of(world: World, sid: str, type_: str) -> list[dict[str, Any]]:
    return [e["data"] for e in world.events(sid) if e["type"] == type_]


def test_usage_is_an_event_type():
    assert "usage" in EVENT_TYPES


async def test_tool_events_and_usage_carry_agent(world: World):
    scenario = {
        "turns": [
            {
                "match": "go",
                "steps": [
                    {"tool": "Bash", "input": {"command": "ls"}, "result": "a"},
                    {
                        "tool": "Read",
                        "input": {"file_path": "x"},
                        "result": "b",
                        "agent": {"id": "ag1", "type": "Explore"},
                    },
                    {"text": "done"},
                ],
            }
        ]
    }
    probe = {"onAgentToolCall": [say({})], "onToolResult": [say({})]}
    m = world.manager(world.cfg(scenario, hooks=probe))
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    router = m.router(sid)
    await router.submit_turn("go")
    await wait_turn(m, sid)
    await router.drain(5)

    calls = {c["tool_name"]: c for c in of(world, sid, "tool_call")}
    results = {c["tool_name"]: c for c in of(world, sid, "tool_result")}
    for table in (calls, results):
        assert table["Bash"]["agent_id"] is None and table["Bash"]["agent_type"] is None
        assert table["Read"]["agent_id"] == "ag1" and table["Read"]["agent_type"] == "Explore"

    usage = of(world, sid, "usage")
    ids = [u["message_id"] for u in usage]
    assert len(ids) == len(set(ids)) and len(ids) >= 3
    assert {u["agent_id"] for u in usage} == {None, "ag1"}
    for u in usage:
        assert set(u) == {
            "message_id",
            "model",
            "agent_id",
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        }
        assert u["model"] == "fake-claude"
