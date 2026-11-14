"""Auto mode unavailable: an event, a status and a fast-failing turn instead of a hang.

The strings are the ones claude prints in place of the "auto mode on" footer (MEMORY 2026-11-01,
permission-switch C1). The fake claude prints them at boot (`boot.footer`) or mid-turn (`print`).
"""

from __future__ import annotations

import pytest
from conftest import World, wait_turn, wait_until

from claude_code_wrapper.manager import CreateSessionRequest
from claude_code_wrapper.session import AUTO_UNAVAILABLE, FatalScanner

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


#: The texts claude 2.1.323 prints when auto mode cannot be used, exactly as its bundle builds
#: them (the reason switch and its "right now" fallback, plus the mid-turn notice), each with the
#: reason the wrapper reports. Evidence: workbench .scratchpad/wrapper-managed-settings/
#: t69-strings.md and t69-probe/ (MEMORY 2026-11-14, T-69).
CASES_2_1_323 = [
    ("auto mode disabled by settings", "settings"),
    ("auto mode is unavailable for your plan", "plan"),
    ("auto mode unavailable while fast mode is on \u00b7 run /fast off", "fast_mode"),
    ("auto mode unavailable for this model", "model"),
    ("auto mode is unavailable right now", "unknown"),
    (
        "Auto mode is unavailable: the server returned no safety verdict for 3 responses in a row",
        "no_safety_verdict",
    ),
]
#: 2.1.323 starts in Manual when auto is unavailable: this footer replaces "auto mode on".
MANUAL_FOOTER_2_1_323 = "\u23f8 manual mode on \u00b7 ? for shortcuts"


@pytest.mark.parametrize(("text", "reason"), CASES_2_1_323)
def test_auto_unavailable_2_1_323_strings(text: str, reason: str) -> None:
    # The notice is drawn right-aligned above the prompt box, the Manual footer below it.
    screen = f"{' ' * 60}{text}\n{'-' * 40}\n> \n{'-' * 40}\n  {MANUAL_FOOTER_2_1_323}\n"
    scanner = FatalScanner(p for p, _ in AUTO_UNAVAILABLE)
    assert [dict(AUTO_UNAVAILABLE)[p] for p in scanner.feed(screen)] == [reason]


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("auto mode disabled by settings", "settings"),
        ("auto mode unavailable for this model", "model"),
    ],
)
async def test_auto_unavailable_manual_start_2_1_323(world: World, text: str, reason: str) -> None:
    # Probed screens (t69-probe/auto-settings, auto-model): Manual footer, no "auto mode on", and
    # the reason notice. The session is ready (not a boot timeout), marked unavailable, and the
    # create turn fails fast instead of running under Manual prompts.
    scenario = {"boot": {**BOOT, "manual_start": text}}
    cfg = world.cfg(scenario, claude={"permission_mode": "auto", "boot": {**QUIET, "dialogs": []}})
    m = world.manager(cfg)
    sid = (await m.create(CreateSessionRequest(prompt="go")))["session_id"]
    turn = await wait_turn(m, sid, "failed")
    assert turn.error == "permission_mode_unavailable"
    (event,) = unavailable_events(world, sid)
    assert event["data"]["reason"] == reason
    snap = m.get(sid)
    assert snap["state"] == "ready"
    assert snap["permission_mode_state"] == "unavailable"
    assert snap["permission_mode_reason"] == reason
    session = m._active[sid].session
    assert session is not None
    assert session.ring.contains("manual mode on")
    assert not session.ring.contains("auto mode on")
    assert not any(e["type"] in ("stall_suspected", "error") for e in world.events(sid))
