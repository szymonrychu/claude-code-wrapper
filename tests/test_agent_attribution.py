"""K5 agent attribution: agent_id/agent_type on tool events and per-message usage events."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from conftest import World, wait_turn
from test_router import Harness, assistant, wait_until, webhook

from claude_code_wrapper.events import EVENT_TYPES
from claude_code_wrapper.manager import CreateSessionRequest


def say(obj: dict[str, Any]) -> dict[str, Any]:
    return {"type": "command", "run": f"printf '%s' '{json.dumps(obj)}'", "blocking": True}


def of(world: World, sid: str, type_: str) -> list[dict[str, Any]]:
    return [e["data"] for e in world.events(sid) if e["type"] == type_]


def test_usage_is_an_event_type():
    assert "usage" in EVENT_TYPES


async def test_tool_events_carry_agent_attribution(world: World):
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
    m = world.manager(world.cfg(scenario))
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
    assert usage
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


SUB = {"agent_id": "ag1", "agent_type": "Explore"}
PRE = {"tool_use_id": "tu1", "tool_name": "Read", "tool_input": {}}
POST = {"tool_use_id": "tu1", "tool_name": "Read", "tool_response": "ok"}
HOOKS = {"onAgentToolCall": [webhook()], "onToolResult": [webhook()]}


async def test_tool_hooks_receive_agent_attribution(tmp_path):
    h = Harness(tmp_path, hooks=HOOKS)
    await h.router.handle_claude_hook("PreToolUse", {**PRE, **SUB})
    await h.router.handle_claude_hook("PostToolUse", {**POST, **SUB})
    pre, post = (p["data"] for p in h.hook_posts)
    for data in (pre, post):
        assert data["agent_id"] == "ag1" and data["agent_type"] == "Explore"
    await h.router.close()


async def test_main_agent_tool_events_have_null_agent(tmp_path):
    h = Harness(tmp_path, hooks=HOOKS)
    await h.router.handle_claude_hook("PreToolUse", PRE)
    await h.router.handle_claude_hook("PostToolUse", POST)
    for data in [h.events("tool_call")[0].data, h.events("tool_result")[0].data] + [
        p["data"] for p in h.hook_posts
    ]:
        assert data["agent_id"] is None and data["agent_type"] is None
    await h.router.close()


async def test_tool_denied_carries_agent_attribution(tmp_path):
    h = Harness(tmp_path, hooks=HOOKS)
    h.reply = {"decision": "deny", "reason": "no"}
    await h.router.handle_claude_hook("PreToolUse", {**PRE, **SUB})
    (ev,) = h.events("tool_denied")
    assert ev.data["agent_id"] == "ag1" and ev.data["agent_type"] == "Explore"
    await h.router.close()


async def test_fail_closed_denied_carries_agent_attribution(tmp_path):
    h = Harness(tmp_path, hooks={"onAgentToolCall": [webhook(on_error="deny")]})

    async def boom(*a, **k):
        raise RuntimeError("down")

    h.dispatcher.fire = boom  # type: ignore[method-assign]
    await h.router.handle_claude_hook("PreToolUse", {**PRE, **SUB})
    (ev,) = h.events("tool_denied")
    assert ev.data["agent_id"] == "ag1" and ev.data["agent_type"] == "Explore"
    await h.router.close()


async def test_usage_event_per_message_once(tmp_path):
    h = Harness(tmp_path)
    h.router.ensure_tailing()
    with h.transcript.open("a") as f:
        f.write(assistant("a", mid="m1"))
        f.write(assistant("a again", mid="m1"))
        f.write(assistant("b", mid="m2", usage={"input_tokens": 7, "output_tokens": 1}))
    await wait_until(lambda: len(h.events("usage")) >= 2)
    await asyncio.sleep(0.2)
    usage = [e.data for e in h.events("usage")]
    assert [u["message_id"] for u in usage] == ["m1", "m2"]
    assert usage[0]["input_tokens"] == 3 and usage[0]["output_tokens"] == 5
    assert usage[1]["input_tokens"] == 7 and usage[0]["agent_id"] is None
    await h.router.close()


async def test_sidechain_usage_event_has_agent_id(tmp_path):
    h = Harness(tmp_path)
    sub = h.transcript.parent / "main" / "subagents"
    sub.mkdir(parents=True)
    h.router.ensure_tailing()
    (sub / "agent-abc.jsonl").write_text(assistant("sub", mid="ms"))
    await wait_until(lambda: h.events("usage"))
    (ev,) = h.events("usage")
    assert ev.data["message_id"] == "ms" and ev.data["agent_id"] == "abc"
    await h.router.close()


async def test_resumed_history_emits_no_usage(tmp_path):
    h = Harness(tmp_path)
    h.session.spec.resume = True
    h.transcript.write_text(assistant("old", mid="old1"))
    h.router.ensure_tailing()
    await asyncio.sleep(0.2)
    assert h.events("usage") == []
    with h.transcript.open("a") as f:
        f.write(assistant("new", mid="new1"))
    await wait_until(lambda: h.events("usage"))
    assert [e.data["message_id"] for e in h.events("usage")] == ["new1"]
    await h.router.close()
