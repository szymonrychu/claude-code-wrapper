"""claude's re-login screen: a typed auth state and fast-failing turns instead of a hang.

The screens are the full lines claude 2.1.323 draws ("<title> <middle dot> Please run /login").
The bare "Please run /login" and the 401 / expired-token texts stay generic fatal patterns.
Placeholders only: no credential material appears in any fixture.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import pytest
from conftest import World, turns_of, wait_turn, wait_until

from claude_code_wrapper.manager import CreateSessionRequest, SessionManager
from claude_code_wrapper.redact import Redactor
from claude_code_wrapper.session import AUTH_FAILED, FatalScanner

pytestmark = pytest.mark.critical

DOT = "·"
CASES = [
    ("Login reached its time limit", "time_limit"),
    ("OAuth token revoked", "revoked"),
    ("Login expired", "expired"),
    ("Not logged in", "not_logged_in"),
]
QUIET = {"timeout_seconds": 5.0, "min_boot_seconds": 0.2, "idle_ready_seconds": 0.4}


def screen(title: str) -> str:
    return f"\n{title} {DOT} Please run /login\n"


def stalls(world: World, sid: str) -> list[dict]:
    return [e for e in world.events(sid) if e["type"] == "stall_suspected"]


@pytest.mark.parametrize(("title", "reason"), CASES)
async def test_auth_screen_mid_turn_fails_turn_and_marks_session(
    world: World, title: str, reason: str
) -> None:
    scenario = {"turns": [{"match": "go", "steps": [{"print": screen(title)}]}]}
    m = world.manager(world.cfg(scenario))
    sid = (await m.create(CreateSessionRequest(prompt="go")))["session_id"]
    turn = await wait_turn(m, sid, "failed")
    assert turn.error == "auth_failed"
    snap = m.get(sid)
    assert snap["auth_state"] == "failed"
    assert snap["auth_reason"] == reason
    assert snap["state"] == "ready"


async def test_auth_screen_in_auto_mode(world: World) -> None:
    scenario = {
        "boot": {"mode": "auto", "min_seconds": 0.1},
        "turns": [{"match": "go", "steps": [{"print": screen("OAuth token revoked")}]}],
    }
    m = world.manager(world.cfg(scenario, claude={"permission_mode": "auto"}))
    sid = (await m.create(CreateSessionRequest(prompt="go")))["session_id"]
    turn = await wait_turn(m, sid, "failed")
    assert turn.error == "auth_failed"
    snap = m.get(sid)
    assert snap["auth_state"] == "failed"
    assert snap["auth_reason"] == "revoked"
    assert snap["state"] == "ready"


async def test_auth_screen_at_boot_fails_first_turn_without_pasting(world: World) -> None:
    boot = {"footer": screen("Login expired").strip(), "bypass_dialog": False, "min_seconds": 0.1}
    scenario = {"boot": boot}
    cfg = world.cfg(scenario, claude={"boot": {**QUIET, "dialogs": []}})
    m = world.manager(cfg)
    sid = (await m.create(CreateSessionRequest(prompt="never typed")))["session_id"]
    turn = await wait_turn(m, sid, "failed")
    assert turn.error == "auth_failed"
    assert m.get(sid)["auth_reason"] == "expired"
    assert stalls(world, sid) == []


async def test_later_turn_fails_fast_after_auth_failure(world: World) -> None:
    scenario = {
        "turns": [
            {"match": "go", "steps": [{"print": screen("OAuth token revoked")}]},
            {"match": "second", "steps": [{"text": "never reached"}]},
        ]
    }
    m = world.manager(world.cfg(scenario))
    sid = (await m.create(CreateSessionRequest(prompt="go")))["session_id"]
    await wait_turn(m, sid, "failed")
    await m.router(sid).submit_turn("second")
    await asyncio.wait_for(
        wait_until(lambda: len([t for t in turns_of(m, sid) if t.state == "failed"]) == 2), 2.0
    )
    last = [t for t in turns_of(m, sid) if t.state == "failed"][-1]
    assert last.error == "auth_failed"
    assert stalls(world, sid) == []


def test_auth_scanner_matches_split_and_escaped() -> None:
    s = FatalScanner(p for p, _ in AUTH_FAILED)
    assert s.feed("Login exp") == []
    assert s.feed("\x1b[1Cired " + DOT + " Please\x1b[2C run /login") == [AUTH_FAILED[2][0]]


async def test_prompt_quoting_auth_text_is_ignored(world: World) -> None:
    quote = f"why does it say Login expired {DOT} Please run /login"
    m = world.manager(world.cfg({}))
    sid = (await m.create(CreateSessionRequest(prompt=quote)))["session_id"]
    await wait_turn(m, sid)
    assert m.get(sid)["auth_state"] is None


async def test_bare_please_run_login_stays_fatal_not_auth(world: World) -> None:
    scenario = {"turns": [{"match": "go", "steps": [{"print": "\nPlease run /login\n"}]}]}
    m = world.manager(world.cfg(scenario))
    sid = (await m.create(CreateSessionRequest(prompt="go")))["session_id"]
    turn = await wait_turn(m, sid, "failed")
    assert turn.error == "Please run /login"
    assert m.get(sid)["auth_state"] is None


async def test_relaunch_clears_auth_state(world: World) -> None:
    scenario = {
        "turns": [
            {
                "match": "go",
                # "print" ends a fake turn, so the screen line is drawn as text, then claude dies.
                "steps": [{"text": screen("OAuth token revoked").strip()}, {"exit": 1}],
            }
        ]
    }
    m = world.manager(world.cfg(scenario))
    sid = (await m.create(CreateSessionRequest(prompt="go")))["session_id"]
    await wait_until(lambda: m.get(sid)["auth_state"] == "failed")
    await wait_until(lambda: m.get(sid)["restarts"] == 1)
    await wait_until(lambda: m.get(sid)["state"] == "ready")
    snap = m.get(sid)
    assert snap["auth_state"] is None
    assert snap["auth_reason"] is None


async def test_healthy_session_has_no_auth_state(world: World) -> None:
    scenario = {"turns": [{"match": "go", "steps": [{"text": "all fine"}]}]}
    m = world.manager(world.cfg(scenario))
    sid = (await m.create(CreateSessionRequest(prompt="go")))["session_id"]
    await wait_turn(m, sid)
    snap = m.get(sid)
    assert snap["auth_state"] is None
    assert snap["auth_reason"] is None


def auth_events(world: World, sid: str) -> list[dict]:
    return [e for e in world.events(sid) if e["type"] == "auth_failed"]


async def test_auth_failed_event_carries_reason_and_redacted_tail(world: World) -> None:
    scenario = {"turns": [{"match": "go", "steps": [{"print": screen("OAuth token revoked")}]}]}
    m = world.manager(world.cfg(scenario))
    sid = (await m.create(CreateSessionRequest(prompt="go")))["session_id"]
    await wait_turn(m, sid, "failed")
    (event,) = auth_events(world, sid)
    data = event["data"]
    assert set(data) == {"reason", "tail"}
    assert data["reason"] == "revoked"
    assert "OAuth token revoked" in data["tail"]


async def test_auth_failed_event_once_per_session(world: World) -> None:
    scenario = {
        "turns": [
            {"match": "go", "steps": [{"print": screen("OAuth token revoked")}]},
            {"match": "second", "steps": [{"print": screen("Login expired")}]},
        ]
    }
    m = world.manager(world.cfg(scenario))
    sid = (await m.create(CreateSessionRequest(prompt="go")))["session_id"]
    await wait_turn(m, sid, "failed")
    await m.router(sid).submit_turn("second")
    await asyncio.wait_for(
        wait_until(lambda: len([t for t in turns_of(m, sid) if t.state == "failed"]) == 2), 2.0
    )
    assert len(auth_events(world, sid)) == 1
    assert m.get(sid)["auth_reason"] == "revoked"


async def test_auth_failed_canary_never_leaks(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    canary = "CANARY-ccw-auth-0001"
    line = f"\nOAuth token revoked {DOT} Please run /login\n{canary}\n"
    scenario = {"turns": [{"match": "go", "steps": [{"print": line}]}]}
    cfg = world.cfg(scenario, claude={"passthrough_env": ["CLAUDE_CODE_OAUTH_TOKEN"]})
    service_env = {"CLAUDE_CODE_OAUTH_TOKEN": canary, "PATH": os.environ["PATH"]}
    m = SessionManager(cfg, internal_url=world.url, service_env=service_env, redactor=Redactor())
    world.managers.append(m)
    sid = (await m.create(CreateSessionRequest(prompt="go")))["session_id"]
    turn = await wait_turn(m, sid, "failed")
    assert len(auth_events(world, sid)) == 1
    assert canary not in json.dumps(world.events(sid))
    assert canary not in json.dumps(m.get(sid))
    assert canary not in (turn.error or "")
    assert canary not in caplog.text
