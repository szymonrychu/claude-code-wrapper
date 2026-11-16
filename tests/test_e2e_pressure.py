"""End to end: the cooperative context-pressure ramp against the scripted fake claude (T-423).

Same harness as test_e2e_lifecycle (real service, real `ccw-hook` and `ccw statusline` relays,
fake claude), with a scenario whose `statusline` steps move the context gauge in tokens. No
wall-clock dependency: the levels follow the gauge, the injections follow tool boundaries.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from test_e2e_lifecycle import (
    BOOT,
    E2E,
    OAUTH_TOKEN,
    until,
)
from test_e2e_lifecycle import e2e as e2e  # noqa: F401  (fixture)

from claude_code_wrapper.config import Config

WINDOW = 200_000
THRESHOLDS = {
    "l1_tokens": 100_000,
    "l2_tokens": 130_000,
    "l3_tokens": 165_000,
    "compact_tokens": 190_000,
    "l1_pct": 50,
    "l2_pct": 65,
    "l3_pct": 82.5,
    "compact_pct": 95,
}
TASK = {"id": "bg1", "type": "shell", "status": "running", "description": "ci watcher"}


def gauge(used: int) -> dict[str, Any]:
    return {
        "statusline": {
            "context_window": {
                "used_percentage": None,
                "context_window_size": WINDOW,
                "current_usage": {"input_tokens": used},
            }
        }
    }


def tool(n: int = 0) -> list[dict[str, Any]]:
    return [{"tool": "Bash", "input": {"command": f"echo {n}"}, "result": "ok"}]


def policy(version: int = 1, **over: Any) -> dict[str, Any]:
    body = {
        "version": version,
        "thresholds": THRESHOLDS,
        "profile": "work",
        "texts": {
            f"l{n}": {"full": f"L{n} Context {{pct}}%", "short": f"L{n} short Context {{pct}}%"}
            for n in (1, 2, 3)
        },
        "reinject_text": "REINJECT Context {pct}%",
    }
    body.update(over)
    return body


def config(w: E2E, scenario: dict[str, Any]) -> Config:
    path = w.tmp / "pressure-scenario.json"
    path.write_text(json.dumps({"boot": {"bypass_dialog": True, "min_seconds": 0.1}, **scenario}))
    return w.config(
        claude={
            "binary": str(w.fake_bin),
            "boot": BOOT,
            "env": {
                "claude_code_oauth_token": OAUTH_TOKEN,
                "extra": {"FAKE_CLAUDE_SCENARIO": str(path), "FAKE_CLAUDE_LOG": str(w.fake_log)},
            },
        },
        pressure={"enabled": True},
    )


async def start(w: E2E, scenario: dict[str, Any], **pol: Any) -> str:
    await w.start(config(w, scenario))
    sid = (await w.create(name="pressure"))["session_id"]
    await w.call("PUT", f"/v1/sessions/{sid}/pressure", json=policy(**pol))
    return sid


async def events(w: E2E, sid: str, types: str) -> list[dict[str, Any]]:
    page = await w.call(
        "GET",
        f"/v1/sessions/{sid}/history",
        params={"types": types, "after_seq": 0, "limit": 500},
    )
    return page.json()["events"]


def spaced(count: int) -> list[dict[str, Any]]:
    """`count` tools, each its own PostToolBatch (a text step closes the batch)."""
    out: list[dict[str, Any]] = []
    for n in range(count):
        out += [*tool(n), {"text": f"step {n}"}]
    return out


async def test_ramp_l0_to_l3_injects_live_pct(e2e: E2E) -> None:  # noqa: F811
    steps = [
        gauge(90_000),
        *tool(),
        gauge(110_000),
        *spaced(20),
        gauge(140_000),
        *tool(),
        *tool(),
        *tool(),
        gauge(170_000),
        *tool(),
    ]
    sid = await start(e2e, {"turns": [{"match": "ramp", "steps": steps}]})
    tid = await e2e.submit(sid, "ramp")
    record = await e2e.finished(sid, tid)
    assert record["state"] == "completed"
    delivered = await events(e2e, sid, "pressure_delivered")
    assert [e["data"]["level"] for e in delivered] == [1, 2, 3], [e["data"] for e in delivered]
    assert [e["data"]["pct"] for e in delivered] == [55.0, 70.0, 85.0]
    transcript = (await e2e.call("GET", f"/v1/sessions/{sid}/transcript")).text
    seen = [
        json.loads(line)["content"]
        for line in transcript.splitlines()
        if '"hook_additional_context"' in line
    ]
    pressure = [t for t in seen if "<context-pressure>" in t]
    assert len(pressure) == 3, seen
    for text, pct in zip(pressure, ("Context 55%", "Context 70%", "Context 85%"), strict=True):
        assert pct in text
    assert not any("Context 45%" in t for t in seen)  # nothing at 90_000


async def test_stop_block_fires_and_is_bounded(e2e: E2E) -> None:  # noqa: F811
    sid = await start(e2e, {"turns": [{"match": "stop", "steps": [gauge(170_000), {"text": "x"}]}]})
    tid = await e2e.submit(sid, "stop")
    record = await e2e.finished(sid, tid)
    assert record["state"] == "completed"
    blocked = await events(e2e, sid, "pressure_stop_blocked")
    assert [e["data"]["count"] for e in blocked] == [1, 2, 3], blocked
    assert {e["data"]["level"] for e in blocked} == {3}
    assert len(await events(e2e, sid, "turn_completed")) == 1


async def test_submitted_state_with_background_task_never_blocks(e2e: E2E) -> None:  # noqa: F811
    scenario = {
        "turns": [
            {
                "match": "bg",
                "stop": {"background_tasks": [TASK]},
                "steps": [gauge(170_000), {"text": "waiting on ci"}],
            }
        ]
    }
    sid = await start(e2e, scenario, handoff_state="submitted")
    await e2e.submit(sid, "bg")
    await until(
        lambda: _stop_seen(e2e),
        "Stop with the background task",
    )
    assert await events(e2e, sid, "pressure_stop_blocked") == []
    state = (await e2e.call("GET", f"/v1/sessions/{sid}/pressure")).json()
    assert state["level"] == 3 and state["stop_blocks_this_turn"] == 0
    inflight = (await e2e.call("GET", f"/v1/sessions/{sid}/inflight")).json()
    assert [t["id"] for t in inflight["background_tasks"]] == ["bg1"], inflight


def _stop_seen(w: E2E) -> bool:
    return bool(w.hooks("onStop") or w.hooks("onTurnComplete"))


async def test_compaction_reinjects_and_resets_level(e2e: E2E) -> None:  # noqa: F811
    steps = [gauge(170_000), {"auto_compact": {"pre_tokens": 170_000}}, {"sleep": 20}]
    sid = await start(e2e, {"turns": [{"match": "compact", "steps": steps}]})
    await e2e.submit(sid, "compact")
    reinjected = await _wait_events(e2e, sid, "pressure_reinjected")
    assert reinjected[0]["data"]["chars"] > 0
    await _wait_events(e2e, sid, "compacted")
    state = (await e2e.call("GET", f"/v1/sessions/{sid}/pressure")).json()
    assert state["level"] == 0, state
    assert state["context"] is None or state["context"].get("used_tokens") in (None, 0)


async def _wait_events(w: E2E, sid: str, types: str) -> list[dict[str, Any]]:
    end = time.monotonic() + 15
    while time.monotonic() < end:
        found = await events(w, sid, types)
        if found:
            return found
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for {types}")
