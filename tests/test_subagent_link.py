"""The Task/Agent tool_use id of a subagent on its events (agents-ui-improvements E2).

claude writes `<session>/subagents/agent-<id>.meta.json` beside the subagent transcript; its
`toolUseId` is the id of the parent's Task/Agent `tool_use` block (E1, verified on real
transcripts). The router emits it as `data.parent_tool_use_id`.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from conftest import World
from test_router import Harness, wait_until

from claude_code_wrapper import router as router_mod
from claude_code_wrapper.manager import CreateSessionRequest
from claude_code_wrapper.router import subagent_meta_path

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts"
AGENT = "a04c938e71f35e132"
TOOL_USE = "toolu_01L1douhGrKCtucQYEnEdowj"


def test_meta_path_sits_beside_the_subagent_transcript() -> None:
    path = subagent_meta_path("/c/projects/workdir/5b0c.jsonl", AGENT)
    assert path == Path(f"/c/projects/workdir/5b0c/subagents/agent-{AGENT}.meta.json")


async def test_subagent_events_carry_the_parent_tool_use_id(tmp_path) -> None:
    h = Harness(tmp_path)
    meta = subagent_meta_path(h.transcript, AGENT)
    meta.parent.mkdir(parents=True)
    shutil.copy(FIXTURES / f"agent-{AGENT}.meta.json", meta)
    await h.router.handle_claude_hook("SubagentStart", {"agent_id": AGENT, "agent_type": "x"})
    await h.router.handle_claude_hook("SubagentStop", {"agent_id": AGENT, "agent_type": "x"})
    (started,) = h.events("subagent_started")
    assert started.data == {"agent_id": AGENT, "agent_type": "x", "parent_tool_use_id": TOOL_USE}
    (stopped,) = h.events("subagent_stopped")
    assert stopped.data["parent_tool_use_id"] == TOOL_USE
    await h.router.close()


async def test_missing_or_bad_meta_is_null(tmp_path) -> None:
    h = Harness(tmp_path)
    await h.router.handle_claude_hook("SubagentStart", {"agent_id": "nometa", "agent_type": "x"})
    meta = subagent_meta_path(h.transcript, "bad")
    meta.parent.mkdir(parents=True)
    meta.write_text("not json")
    await h.router.handle_claude_hook("SubagentStart", {"agent_id": "bad", "agent_type": "x"})
    assert [e.data["parent_tool_use_id"] for e in h.events("subagent_started")] == [None] * 2
    await h.router.close()


async def test_agent_id_cannot_reach_a_file_outside_subagents(tmp_path) -> None:
    # A decoy meta file reachable only by walking out of `subagents/` with "..".
    h = Harness(tmp_path)
    evil = "x/../../../decoy"
    path = subagent_meta_path(h.transcript, evil)
    (path.parent.parent.parent / "subagents" / "agent-x").mkdir(parents=True)
    decoy = h.transcript.parent / "decoy.meta.json"
    decoy.write_text(json.dumps({"toolUseId": "toolu_DECOY"}))
    assert json.loads(path.read_text())["toolUseId"] == "toolu_DECOY"  # the path does resolve
    await h.router.handle_claude_hook("SubagentStart", {"agent_id": evil, "agent_type": "x"})
    (started,) = h.events("subagent_started")
    assert started.data["parent_tool_use_id"] is None
    await h.router.close()


async def test_fake_claude_subagent_links_to_its_task_call(world: World) -> None:
    scenario = {
        "turns": [
            {
                "match": "delegate",
                "steps": [{"subagent": {"id": AGENT, "text": "done", "tool_use_id": TOOL_USE}}],
            }
        ]
    }
    m = world.manager(world.cfg(scenario))
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    await m.router(sid).submit_turn("delegate it")
    await world.wait_turn(m, sid)
    (started,) = [e["data"] for e in world.events(sid) if e["type"] == "subagent_started"]
    assert started["agent_id"] == AGENT and started["parent_tool_use_id"] == TOOL_USE
    session_dir = Path(m.router(sid).session.transcript_path).with_suffix("")
    meta = json.loads((session_dir / "subagents" / f"agent-{AGENT}.meta.json").read_text())
    assert meta["toolUseId"] == TOOL_USE


async def test_meta_written_after_start_emits_a_link_event(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(router_mod, "SUBAGENT_LINK_POLL_SECONDS", 0.02)
    h = Harness(tmp_path)
    await h.router.handle_claude_hook("SubagentStart", {"agent_id": AGENT, "agent_type": "x"})
    (started,) = h.events("subagent_started")
    assert started.data["parent_tool_use_id"] is None
    assert h.events("subagent_linked") == []
    meta = subagent_meta_path(h.transcript, AGENT)
    meta.parent.mkdir(parents=True)
    shutil.copy(FIXTURES / f"agent-{AGENT}.meta.json", meta)
    await wait_until(lambda: h.events("subagent_linked"))
    (linked,) = h.events("subagent_linked")
    assert linked.data == {"agent_id": AGENT, "parent_tool_use_id": TOOL_USE}
    await h.router.close()


async def test_link_wait_is_bounded_and_skipped_when_known(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(router_mod, "SUBAGENT_LINK_POLL_SECONDS", 0.02)
    monkeypatch.setattr(router_mod, "SUBAGENT_LINK_WAIT_SECONDS", 0.1)
    h = Harness(tmp_path)
    await h.router.handle_claude_hook("SubagentStart", {"agent_id": "never", "agent_type": "x"})
    meta = subagent_meta_path(h.transcript, AGENT)
    meta.parent.mkdir(parents=True)
    shutil.copy(FIXTURES / f"agent-{AGENT}.meta.json", meta)
    await h.router.handle_claude_hook("SubagentStart", {"agent_id": AGENT, "agent_type": "x"})
    await asyncio.sleep(0.3)
    assert h.events("subagent_linked") == []  # "never" gave up; AGENT was linked at start
    await h.router.close()


async def test_link_wait_ends_with_the_subagent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(router_mod, "SUBAGENT_LINK_POLL_SECONDS", 0.02)
    h = Harness(tmp_path)
    await h.router.handle_claude_hook("SubagentStart", {"agent_id": AGENT, "agent_type": "x"})
    await h.router.handle_claude_hook("SubagentStop", {"agent_id": AGENT, "agent_type": "x"})
    meta = subagent_meta_path(h.transcript, AGENT)
    meta.parent.mkdir(parents=True)
    shutil.copy(FIXTURES / f"agent-{AGENT}.meta.json", meta)
    await asyncio.sleep(0.2)
    assert h.events("subagent_linked") == []
    await asyncio.wait_for(h.router.drain(), 1.0)  # the wait is not a drained task
    await h.router.close()
