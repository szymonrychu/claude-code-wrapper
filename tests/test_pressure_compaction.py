"""Compaction steering: PreCompact stdout and SessionStart(compact) re-injection."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_pressure_injection import TH, make, policy, status
from test_router import Harness

from claude_code_wrapper.bootstrap.settings import (
    CLAUDE_HOOK_EVENTS,
    RELAY_MATCHERS,
    render_settings,
)
from claude_code_wrapper.config import Config
from claude_code_wrapper.pressure import PressurePolicy

INSTR = "Keep the task, the todo and the in-flight state."
REINJ = "After compaction: you are at {pct}% context, restore your task and todo."


def pol(**kw) -> PressurePolicy:
    return policy().model_copy(update={"compact_instructions": INSTR, "reinject_text": REINJ, **kw})


def with_policy(tmp_path: Path, **kw) -> Harness:
    h = make(tmp_path, used=None)
    h.state.put(pol(**kw))
    return h


def sub(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


def ctx(r: dict) -> str:
    return json.loads(r["stdout"])["hookSpecificOutput"]["additionalContext"]


async def pre(h: Harness, trigger: str = "auto"):
    return await h.router.handle_claude_hook("PreCompact", {"trigger": trigger})


async def start(h: Harness, source: str, sid: str = "new-sid"):
    return await h.router.handle_claude_hook("SessionStart", {"source": source, "session_id": sid})


async def test_precompact_returns_policy_instructions(tmp_path):
    h = with_policy(tmp_path)
    r = await pre(h)
    assert r == {"stdout": INSTR, "stderr": "", "exit_code": 0}


async def test_precompact_without_policy_allows_empty(tmp_path):
    h = make(tmp_path, used=None, pol=False)
    assert (await pre(h))["stdout"] == ""
    h2 = with_policy(sub(tmp_path, "x"), compact_instructions="")
    assert (await pre(h2))["stdout"] == ""
    h3 = Harness(sub(tmp_path, "y"))  # no pressure state at all
    assert (await pre(h3))["stdout"] == ""


async def test_precompact_instructions_capped_9000_chars(tmp_path):
    h = with_policy(tmp_path, compact_instructions="x" * 9000)
    assert len((await pre(h, "manual"))["stdout"]) == 9000
    # The router passes the policy text through untruncated at the cap.
    assert (await pre(h, "auto"))["stdout"] == "x" * 9000
    with pytest.raises(ValueError):
        PressurePolicy.model_validate({**pol().model_dump(), "compact_instructions": "x" * 9001})


async def test_session_start_compact_reinjects(tmp_path):
    h = with_policy(tmp_path)
    r = await start(h, "compact", sid=h.router.info.claude_session_id)
    assert r["exit_code"] == 0
    body = json.loads(r["stdout"])["hookSpecificOutput"]
    assert body["hookEventName"] == "SessionStart"
    assert body["additionalContext"] == REINJ.replace("{pct}", "?")
    assert h.router.info.claude_session_id != "new-sid"
    (ev,) = [e.data for e in h.events("pressure_reinjected")]
    assert ev == {"chars": len(body["additionalContext"])}


async def test_session_start_compact_after_high_context_renders_unknown(tmp_path):
    h = with_policy(tmp_path)
    status(h, 190_000)
    assert h.router.context is not None
    r = await start(h, "compact")
    assert ctx(r) == REINJ.replace("{pct}", "?")
    assert h.router.context is None


async def test_session_start_clear_path_unchanged(tmp_path):
    h = with_policy(tmp_path, reinject_text="")
    status(h, 110_000)
    adopted = []

    def adopt(new_id, path):
        adopted.append((new_id, path))
        return h.router.info.claude_session_id

    h.session.adopt_claude_session = adopt
    h.session.transcript_path = str(h.tmp / "t" / "new-sid.jsonl")
    h.router._current = SimpleNamespace(name="clear", state="running")
    r = await h.router.handle_claude_hook(
        "SessionStart", {"source": "clear", "session_id": "new-sid", "transcript_path": "p"}
    )
    assert r == {"stdout": "", "stderr": "", "exit_code": 0}
    assert adopted == [("new-sid", "p")]
    assert h.router.info.claude_session_id == "new-sid"
    assert h.router.context is None
    (ev,) = h.events("session_cleared")
    assert ev.data["claude_session_id"] == "new-sid"
    assert not h.events("pressure_reinjected")


async def test_session_start_compact_never_adopts_session_id(tmp_path):
    h = with_policy(tmp_path)
    before = h.router.info.claude_session_id
    await start(h, "compact", sid="other-sid")
    assert h.router.info.claude_session_id == before


async def test_session_start_compact_without_policy_empty(tmp_path):
    h = make(tmp_path, used=None, pol=False)
    r = await start(h, "compact")
    assert r == {"stdout": "", "stderr": "", "exit_code": 0}
    h2 = with_policy(sub(tmp_path, "x"), reinject_text="")
    assert (await start(h2, "compact"))["stdout"] == ""
    assert not h2.events("pressure_reinjected")


async def test_session_start_other_sources_unchanged(tmp_path):
    h = with_policy(tmp_path)
    for source in ("startup", "resume"):
        assert (await start(h, source))["stdout"] == ""


def test_relay_matcher_clear_or_compact(tmp_path):
    assert RELAY_MATCHERS["SessionStart"] == "clear|compact"
    doc = render_settings(tmp_path, Config(), relay_command="relay")
    (group,) = doc["hooks"]["SessionStart"]
    assert group["matcher"] == "clear|compact"
    assert "SessionStart" in CLAUDE_HOOK_EVENTS and TH
