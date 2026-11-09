"""Permission broker: single-assignment pendings for relayed PermissionRequest hooks."""

from __future__ import annotations

import asyncio
import datetime

from claude_code_wrapper.permissions import (
    RESOLVED_MEMORY,
    TOOL_INPUT_MAX_BYTES,
    Decision,
    PermissionBroker,
)
from claude_code_wrapper.redact import Redactor

SECRET = "s3cr3t-value-123"


def broker() -> PermissionBroker:
    return PermissionBroker(Redactor([SECRET]))


def payload(command="ls", agent_id=None, tool="Bash", **extra):
    out = {"tool_name": tool, "tool_input": {"command": command}, **extra}
    if agent_id is not None:
        out["agent_id"] = agent_id
    return out


def parse(ts: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))


async def test_open_and_view():
    b = broker()
    p = b.open(payload(permission_suggestions=[{"type": "addRules"}]), 90)
    assert p.request_id.startswith("perm_") and len(p.request_id) == len("perm_") + 12
    assert (parse(p.expires_at) - parse(p.since)).total_seconds() == 90
    v = p.view()
    assert set(v) == {
        "request_id",
        "tool_name",
        "tool_input",
        "truncated",
        "suggestions",
        "agent_id",
        "since",
        "expires_at",
    }
    assert v["tool_name"] == "Bash" and v["agent_id"] is None and v["truncated"] is False
    assert v["suggestions"] == [{"type": "addRules"}]
    assert '"command": "ls"' in v["tool_input"]
    assert len(b) == 1 and b.first() is p and b.get(p.request_id) is p


async def test_open_without_suggestions():
    assert broker().open(payload(), 10).suggestions == []


async def test_resolve_first_wins():
    b = broker()
    p = b.open(payload(), 10)
    first = Decision("allow", "user")
    assert b.resolve(p.request_id, first) is True
    assert b.resolve(p.request_id, Decision("deny", "timeout")) is False
    assert p.future.result() == first
    assert len(b) == 0 and b.first() is None and b.get(p.request_id) is None
    assert b.was_resolved(p.request_id)


async def test_resolve_unknown_false():
    b = broker()
    assert b.resolve("perm_nope", Decision("deny", "gone")) is False
    assert not b.was_resolved("perm_nope")


async def test_was_resolved_bounded():
    b = broker()
    ids = []
    for _ in range(RESOLVED_MEMORY + 1):
        p = b.open(payload(), 10)
        ids.append(p.request_id)
        b.resolve(p.request_id, Decision("allow", "user"))
    assert not b.was_resolved(ids[0])
    assert b.was_resolved(ids[1]) and b.was_resolved(ids[-1])


async def test_view_redacts_before_truncating():
    b = broker()
    p = b.open(
        {"tool_name": "Bash", "tool_input": {"command": "x" * 5000 + SECRET, "u": "e\u00e9" * 10}},
        10,
    )
    v = p.view()
    assert SECRET not in v["tool_input"]
    raw = v["tool_input"].encode("utf-8")
    assert len(raw) <= TOOL_INPUT_MAX_BYTES
    raw.decode("utf-8")
    assert v["truncated"] is True


async def test_view_redacts_secret_straddling_the_cut():
    b = broker()
    p = b.open({"tool_name": "Bash", "tool_input": {"command": "x" * 4080 + SECRET}}, 10)
    v = p.view()
    assert SECRET[:6] not in v["tool_input"] and v["truncated"] is True


async def test_cut_on_utf8_boundary():
    p = broker().open({"tool_name": "Write", "tool_input": {"text": "\u00e9" * 3000}}, 10)
    v = p.view()
    assert len(v["tool_input"].encode("utf-8")) <= TOOL_INPUT_MAX_BYTES
    assert v["tool_input"].endswith("\u00e9") and v["truncated"] is True


async def test_small_input_not_truncated():
    p = broker().open({"tool_name": "Bash", "tool_input": {"command": "ls"}}, 10)
    assert p.view()["truncated"] is False


async def test_raw_input_never_serialised():
    p = broker().open(payload(command=SECRET), 10)
    assert SECRET not in str(p.view())
    assert p.tool_input == {"command": SECRET}


async def test_fifo_first_and_match_tool():
    b = broker()
    a = b.open(payload("ls"), 10)
    c = b.open(payload("pwd"), 10)
    assert b.first() is a and len(b) == 2
    assert b.match_tool("Bash", {"command": "pwd"}, None) is c
    assert b.match_tool("Bash", {"command": "pwd"}, "sub1") is None
    assert b.match_tool("Read", {"command": "pwd"}, None) is None
    assert b.match_tool("Bash", {"command": "other"}, None) is None
    b.resolve(a.request_id, Decision("allow", "terminal"))
    assert b.first() is c


async def test_match_tool_returns_oldest_of_equal():
    b = broker()
    a = b.open(payload("ls"), 10)
    b.open(payload("ls"), 10)
    assert b.match_tool("Bash", {"command": "ls"}, None) is a


async def test_of_agent_and_resolve_all():
    b = broker()
    m = b.open(payload("a"), 10)
    s1 = b.open(payload("b", agent_id="sub1"), 10)
    s2 = b.open(payload("c", agent_id="sub1"), 10)
    assert b.of_agent(None) == [m]
    assert b.of_agent("sub1") == [s1, s2]
    assert b.of_agent("zzz") == []
    ids = b.resolve_all(Decision("deny", "gone", "session ended"))
    assert ids == [m.request_id, s1.request_id, s2.request_id]
    assert len(b) == 0
    assert m.future.result() == Decision("deny", "gone", "session ended")
    assert b.resolve_all(Decision("deny", "gone")) == []


async def test_secret_straddling_byte_4096_is_redacted_before_the_cut():
    b = broker()
    p = b.open(
        {"tool_name": "Bash", "tool_input": {"command": "x" * 4070 + SECRET + "y" * 200}}, 10
    )
    v = p.view()
    assert v["truncated"] is True
    assert SECRET[:8] not in v["tool_input"]


async def test_cancelled_future_still_records_resolution():
    b = broker()
    p = b.open(payload(), 10)
    waiter = asyncio.ensure_future(p.future)
    await asyncio.sleep(0)
    waiter.cancel()
    await asyncio.sleep(0)
    assert p.future.cancelled()
    assert b.resolve(p.request_id, Decision("deny", "gone")) is False
    assert b.was_resolved(p.request_id)
    assert b.first() is None
