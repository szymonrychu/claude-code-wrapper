"""Auto mode unavailable: an event, a status and a fast-failing turn instead of a hang.

The strings are the ones claude prints in place of the "auto mode on" footer (MEMORY 2026-11-01,
permission-switch C1). The fake claude prints them at boot (`boot.footer`) or mid-turn (`print`).
"""

from __future__ import annotations

import pytest
from conftest import World, wait_turn, wait_until

from claude_code_wrapper.manager import CreateSessionRequest

CASES = [
    ("auto mode disabled by settings", "settings"),
    ("auto mode is unavailable for your plan", "plan"),
    ("auto mode unavailable while fast mode is on · run /fast off", "fast_mode"),
    ("auto mode unavailable for this model", "model"),
    ("auto mode is unavailable right now", "unknown"),
    (
        "Auto mode is unavailable: the server returned no safety verdict for 3 responses in a row",
        "no_safety_verdict",
    ),
]
BOOT = {"mode": "auto", "min_seconds": 0.1}
QUIET = {"timeout_seconds": 5.0, "min_boot_seconds": 0.2, "idle_ready_seconds": 0.4}


def unavailable_events(world: World, sid: str) -> list[dict]:
    return [e for e in world.events(sid) if e["type"] == "permission_mode_unavailable"]


@pytest.mark.parametrize(("text", "reason"), CASES)
async def test_unavailable_mid_turn_fails_fast(world: World, text: str, reason: str) -> None:
    scenario = {"boot": BOOT, "turns": [{"match": "go", "steps": [{"print": f"\n{text}\n"}]}]}
    m = world.manager(world.cfg(scenario, claude={"permission_mode": "auto"}))
    sid = (await m.create(CreateSessionRequest(prompt="go")))["session_id"]
    turn = await wait_turn(m, sid, "failed")
    assert turn.error == "permission_mode_unavailable"
    await wait_until(lambda: unavailable_events(world, sid))
    (event,) = unavailable_events(world, sid)
    assert event["data"]["reason"] == reason
    snap = m.get(sid)
    assert snap["permission_mode_state"] == "unavailable"
    assert snap["permission_mode_reason"] == reason
    assert snap["state"] == "ready"  # the slot is free again, not wedged


@pytest.mark.parametrize(("text", "reason"), CASES)
async def test_unavailable_at_boot_fails_the_turn_without_pasting(
    world: World, text: str, reason: str
) -> None:
    scenario = {"boot": {**BOOT, "footer": text}}
    cfg = world.cfg(
        scenario,
        claude={"permission_mode": "auto", "boot": {**QUIET, "dialogs": []}},
    )
    m = world.manager(cfg)
    sid = (await m.create(CreateSessionRequest(prompt="never typed")))["session_id"]
    turn = await wait_turn(m, sid, "failed")
    assert turn.error == "permission_mode_unavailable"
    (event,) = unavailable_events(world, sid)
    assert event["data"]["reason"] == reason
    assert m.get(sid)["permission_mode_state"] == "unavailable"
    assert not any(e["type"] == "stall_suspected" for e in world.events(sid))


async def test_healthy_auto_emits_nothing(world: World) -> None:
    scenario = {"boot": BOOT, "turns": [{"match": "go", "steps": [{"text": "all fine"}]}]}
    m = world.manager(world.cfg(scenario, claude={"permission_mode": "auto"}))
    sid = (await m.create(CreateSessionRequest(prompt="go")))["session_id"]
    await wait_turn(m, sid)
    assert unavailable_events(world, sid) == []
    snap = m.get(sid)
    assert snap["permission_mode_state"] is None
    assert snap["permission_mode_reason"] is None


async def test_bypass_mode_ignores_the_text(world: World) -> None:
    text = "auto mode is unavailable for your plan"
    scenario = {
        "boot": {"bypass_dialog": True, "min_seconds": 0.1},
        "turns": [{"match": "go", "steps": [{"text": text}]}],
    }
    m = world.manager(world.cfg(scenario))
    sid = (await m.create(CreateSessionRequest(prompt="go")))["session_id"]
    await wait_turn(m, sid)
    assert unavailable_events(world, sid) == []
    assert m.get(sid)["permission_mode_state"] is None


async def test_prompt_quoting_the_string_is_not_unavailable(world: World) -> None:
    quote = "why does it say auto mode is unavailable for your plan"
    m = world.manager(world.cfg({"boot": BOOT}, claude={"permission_mode": "auto"}))
    sid = (await m.create(CreateSessionRequest(prompt=quote)))["session_id"]
    await wait_turn(m, sid)
    assert unavailable_events(world, sid) == []
