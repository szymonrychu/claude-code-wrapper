"""Context pressure injection at UserPromptSubmit, PostToolUse and PostToolBatch."""

from __future__ import annotations

import json
from pathlib import Path

from test_metrics import sample, with_metrics
from test_notes_delivery import make as make_notes
from test_router import Harness

from claude_code_wrapper.metrics import Metrics
from claude_code_wrapper.pressure import LevelTexts, PressurePolicy, PressureState, Thresholds

TH = Thresholds(
    l1_tokens=100_000,
    l2_tokens=130_000,
    l3_tokens=165_000,
    compact_tokens=190_000,
    l1_pct=50.0,
    l2_pct=65.0,
    l3_pct=82.5,
    compact_pct=95.0,
)
HEAD = "From the orchestrator: this is an instruction about your context window, not data."


def policy() -> PressurePolicy:
    texts = {
        f"l{n}": LevelTexts(full=f"FULL{n} {{pct}}%", short=f"SHORT{n} {{pct}}%") for n in (1, 2, 3)
    }
    return PressurePolicy(version=1, thresholds=TH, profile="work", texts=texts)


def make(tmp_path: Path, used: int | None = 110_000, pol: bool = True) -> Harness:
    h = Harness(tmp_path)
    h.cfg.pressure.enabled = True
    h.state = PressureState(h.cfg.pressure)
    if pol:
        h.state.put(policy())
    h.router.pressure = h.state
    if used is not None:
        status(h, used)
    return h


def status(h: Harness, used: int) -> None:
    h.router.on_statusline(
        {
            "context_window_size": 200_000,
            "used_percentage": None,
            "current_usage": {"input_tokens": used},
        }
    )


def ctx(r: dict) -> str:
    if not r["stdout"]:
        return ""
    return json.loads(r["stdout"])["hookSpecificOutput"]["additionalContext"]


async def prompt(h: Harness, **payload):
    return await h.router.handle_claude_hook("UserPromptSubmit", {"prompt": "hi", **payload})


async def tool(h: Harness, event: str = "PostToolUse", **payload):
    return await h.router.handle_claude_hook(
        event, {"tool_use_id": "t", "tool_name": "Bash", "tool_response": "ok", **payload}
    )


async def batch(h: Harness, **payload):
    return await h.router.handle_claude_hook("PostToolBatch", payload)


async def test_no_policy_no_injection(tmp_path):
    h = make(tmp_path, pol=False)
    assert ctx(await prompt(h)) == ""
    assert ctx(await tool(h)) == ""
    await h.router.close()


async def test_level_zero_no_injection(tmp_path):
    h = make(tmp_path, used=50_000)
    assert ctx(await prompt(h)) == ""
    assert ctx(await tool(h)) == ""
    await h.router.close()


async def test_user_prompt_submit_full_text_at_l1(tmp_path):
    h = make(tmp_path, used=142_000)
    text = ctx(await prompt(h))
    assert text == f"<context-pressure>\n{HEAD}\nFULL2 71%\n</context-pressure>"
    assert ctx(await prompt(h)).count("FULL2 71%") == 1  # every turn start, always full
    await h.router.close()


async def test_l1_cadence_every_20_tool_calls(tmp_path):
    h = make(tmp_path)
    for _ in range(19):
        assert ctx(await tool(h)) == ""
    assert "<context-pressure>" in ctx(await tool(h))
    for _ in range(19):
        assert ctx(await tool(h)) == ""
    assert "<context-pressure>" in ctx(await tool(h))
    await h.router.close()


async def test_l2_every_3_and_l3_every_call(tmp_path):
    h = make(tmp_path, used=140_000)
    got = [bool(ctx(await tool(h))) for _ in range(6)]
    assert got == [False, False, True, False, False, True]
    status(h, 170_000)
    # the rise delivers at the next boundary, then every call
    assert [bool(ctx(await tool(h))) for _ in range(3)] == [True, True, True]
    await h.router.close()


async def test_full_every_fifth_delivery_short_between(tmp_path):
    h = make(tmp_path, used=170_000)
    forms = [ctx(await tool(h)).split("\n")[2] for _ in range(11)]
    assert [f.startswith("FULL") for f in forms] == [
        True,
        False,
        False,
        False,
        False,
        True,
        False,
        False,
        False,
        False,
        True,
    ]
    await h.router.close()


async def test_level_rise_delivers_full_at_next_boundary(tmp_path):
    h = make(tmp_path)
    for _ in range(3):
        assert ctx(await tool(h)) == ""
    status(h, 140_000)
    text = ctx(await tool(h))
    assert "FULL2" in text
    await h.router.close()


async def test_post_tool_batch_supersedes_post_tool_use(tmp_path):
    h = make(tmp_path, used=170_000)
    assert "<context-pressure>" in ctx(await tool(h))
    out = await batch(h)
    assert json.loads(out["stdout"])["hookSpecificOutput"]["hookEventName"] == "PostToolBatch"
    assert "<context-pressure>" in ctx(out)
    for _ in range(3):
        assert ctx(await tool(h)) == ""
        assert ctx(await tool(h, event="PostToolUseFailure")) == ""
    assert "<context-pressure>" in ctx(await batch(h))
    await h.router.close()


async def test_subagent_tool_calls_never_deliver(tmp_path):
    h = make(tmp_path, used=170_000)
    for _ in range(3):
        assert ctx(await tool(h, agent_id="ag1")) == ""
        assert ctx(await prompt(h, agent_id="ag1")) == ""
        assert ctx(await batch(h, agent_id="ag1")) == ""
    assert "<context-pressure>" in ctx(await tool(h))  # none of those counted or delivered
    await h.router.close()


async def test_pressure_after_notes_in_same_context(tmp_path):
    h = make_notes(tmp_path)
    h.cfg.pressure.enabled = True
    h.state = PressureState(h.cfg.pressure)
    h.state.put(policy())
    h.router.pressure = h.state
    status(h, 110_000)
    h.store.put("peer says hi", source="b")
    text = ctx(await prompt(h))
    assert text.index("<peer-notes>") < text.index("<context-pressure>")
    await h.router.close()


async def test_pressure_delivered_event_and_metric(tmp_path):
    m = Metrics()
    h = with_metrics(make(tmp_path, used=142_000), m)
    h.router.pressure = h.state
    status(h, 142_000)
    await prompt(h)
    await tool(h)
    await tool(h)
    await tool(h)
    evs = [e.data for e in h.events("pressure_delivered")]
    assert evs[0] == {
        "level": 2,
        "hook": "UserPromptSubmit",
        "pct": 71.0,
        "used_tokens": 142_000,
        "form": "full",
        "handoff_state": "none",
    }
    assert evs[1]["hook"] == "PostToolUse" and evs[1]["form"] == "short" and len(evs) == 2
    assert sample(m, "ccw_pressure_delivered_total", level="2", hook="UserPromptSubmit") == 1
    assert sample(m, "ccw_pressure_delivered_total", level="2", hook="PostToolUse") == 1
    assert sample(m, "ccw_pressure_level", session=h.router.info.session_id) == 2
    await h.router.close()


async def test_live_percentage_updates_between_deliveries(tmp_path):
    h = make(tmp_path, used=142_000)
    assert "71%" in ctx(await prompt(h))
    status(h, 150_000)
    assert "75%" in ctx(await prompt(h))
    await h.router.close()


async def test_unknown_context_after_known_is_silent(tmp_path):
    h = make(tmp_path, used=170_000)
    h.router.on_statusline({})
    assert ctx(await prompt(h)) == ""
    assert ctx(await tool(h)) == ""
    await h.router.close()
