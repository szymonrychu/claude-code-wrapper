"""Delivery lanes on `POST /v1/sessions/{id}/turns` (agents-ui-improvements C1-C5)."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import httpx
import pytest
from conftest import World, jsonl
from fastapi.testclient import TestClient
from test_api_turns import (  # noqa: F401 - fixtures
    SID,
    TOKEN,
    H,
    StubManager,
    _live_app,
    client,
    stub,
)
from test_session_turns import Harness, harness, wait_until  # noqa: F401 - fixture

from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.api.turns import LANES, TurnRequest
from claude_code_wrapper.manager import CreateSessionRequest, InvalidState
from claude_code_wrapper.pty_proc import paste_bytes
from claude_code_wrapper.session import ESC_SPACING_SECONDS, TurnRejected


def post(client: TestClient, body: dict[str, Any]):  # noqa: F811
    return client.post(f"/v1/sessions/{SID}/turns", json=body)


def test_lane_values_and_default_regular() -> None:
    assert LANES == ("idle", "regular", "priority")
    assert TurnRequest(text="hi").lane == "regular"
    for lane in LANES:
        assert TurnRequest(text="hi", lane=lane).lane == lane


@pytest.mark.parametrize("lane", ["urgent", "", "IDLE", 1, None])
def test_unknown_lane_is_422(client: TestClient, stub: StubManager, lane: Any) -> None:  # noqa: F811
    r = post(client, {"text": "hi", "lane": lane})
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "invalid_request" and "lane" in body["message"]
    assert stub.router_obj.calls == []


# ------------------------------------------------------------------ C2: regular lane


def message_tag(message_id: str) -> str:
    return f"\n\n<!-- ccw message: {message_id} -->"


async def test_queue_prompt_pastes_into_the_running_turn(harness: Harness) -> None:  # noqa: F811
    session = await harness.start({"turns": [{"match": "slow", "steps": [{"sleep": 30}]}]})
    record = await session.submit("slow one")
    harness.writes.clear()
    msg = await session.queue_prompt("steer left", lane="regular", origin="client")
    assert msg.message_id.startswith("msg-") and msg.lane == "regular"
    assert msg.turn_id == record.turn_id and msg.origin == "client" and msg.queued_at
    # The paste with the message marker, then one CR: no ESC, no turn marker.
    assert harness.writes == [paste_bytes("steer left" + message_tag(msg.message_id)), b"\r"]
    assert session.state == "busy" and session.current_turn is record
    assert record.state == "running"
    assert [r.turn_id for r in harness.store.list()] == [record.turn_id]
    assert session.queued_message(msg.message_id) is msg
    # A client-chosen id is used as is.
    own = await session.queue_prompt("more", lane="regular", origin="peer", message_id="m-7")
    assert own.message_id == "m-7" and own.origin == "peer"


async def test_queue_prompt_needs_a_running_turn(harness: Harness) -> None:  # noqa: F811
    session = harness.build()
    with pytest.raises(TurnRejected) as exc:
        await session.queue_prompt("x", lane="regular", origin="client")
    assert exc.value.reason == "booting"
    await session.start()
    harness.spy()
    with pytest.raises(TurnRejected) as exc:  # ready: nothing to queue into
        await session.queue_prompt("x", lane="regular", origin="client")
    assert exc.value.reason == "busy"
    with pytest.raises(TurnRejected) as exc:
        await session.queue_prompt("/compact", lane="regular", origin="client")
    assert exc.value.reason == "slash_command"
    await session.stop(grace=1.0)
    with pytest.raises(TurnRejected) as exc:
        await session.queue_prompt("x", lane="regular", origin="client")
    assert exc.value.reason == "dead"
    assert harness.writes == []


SLOW = {"turns": [{"match": "slow", "steps": [{"sleep": 3}, {"text": "done"}]}]}


async def test_regular_lane_over_http(world: World) -> None:
    m, sid, c = await _live_app(world)
    async with c:
        # Idle: a normal turn.
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "hello"})
        assert r.status_code == 202
        body = r.json()
        assert body == {
            "turn_id": body["turn_id"],
            "state": "running",
            "lane": "regular",
            "queued": False,
        }
        await world.wait_turn(m, sid)


async def test_regular_lane_busy_queues(world: World) -> None:
    cfg = world.cfg(SLOW, server={"auth_token": TOKEN})
    m = world.manager(cfg)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    transport = httpx.ASGITransport(app=create_app(m, cfg))
    keys = world.session_dir(sid) / "config" / "fake-keys.jsonl"
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=H) as c:
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "slow one", "lane": "idle"})
        assert r.status_code == 202 and r.json()["queued"] is False
        running = r.json()["turn_id"]
        # Busy, idle lane: refused as before.
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "x", "lane": "idle"})
        assert r.status_code == 409 and r.json()["error"]["code"] == "busy"
        # Busy, regular lane (the default): typed into the running turn.
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "also do this"})
        assert r.status_code == 202
        body = r.json()
        assert body["queued"] is True and body["lane"] == "regular"
        assert body["turn_id"] == running and body["state"] == "running"
        assert body["message_id"].startswith("msg-")
        await world.wait_turn(m, sid)
    assert jsonl(keys) == []  # no ESC
    events = world.events(sid)
    queued = [e["data"] for e in events if e["type"] == "prompt_queued"]
    assert queued == [
        {
            "message_id": body["message_id"],
            "lane": "regular",
            "turn_id": running,
            "origin": "client",
        }
    ]
    users = [e["data"] for e in events if e["type"] == "user_message"]
    assert [u["text"] for u in users] == ["slow one", "also do this"]
    assert users[1]["turn_id"] == running
    # One turn: the queued message did not start one.
    assert [t.turn_id for t in m.open_turns(sid).list()] == [running]


def test_regular_lane_not_live_is_409(client: TestClient, stub: StubManager) -> None:  # noqa: F811
    for reason in ("booting", "dead"):
        stub.router_obj.raise_on = TurnRejected(reason)
        for lane in ("regular", "priority"):
            r = post(client, {"text": "a", "lane": lane})
            assert r.status_code == 409 and r.json()["error"]["code"] == reason
        r = post(client, {"text": "a", "lane": "idle"})
        assert r.status_code == 503 and r.json()["error"]["code"] == reason
    stub.router_obj.raise_on = None
    stub.router_error = InvalidState(SID, "stopped")
    r = post(client, {"text": "a"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "dead"
    r = post(client, {"text": "a", "lane": "idle"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "dead"


def test_lane_reaches_the_router(client: TestClient, stub: StubManager) -> None:  # noqa: F811
    for lane in ("idle", "regular", "priority"):
        assert post(client, {"text": "a", "lane": lane}).status_code == 202
    assert stub.router_obj.lanes == ["idle", "regular", "priority"]


# ------------------------------------------------------------------ C3: priority lane


async def _lane_app(world: World, scenario: dict, **sections: Any):
    cfg = world.cfg(scenario, server={"auth_token": TOKEN}, **sections)
    m = world.manager(cfg)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    transport = httpx.ASGITransport(app=create_app(m, cfg))
    return m, sid, httpx.AsyncClient(transport=transport, base_url="http://t", headers=H)


def _ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


PRIORITY = {"turns": [{"match": "slow|first|second", "steps": [{"sleep": 30}]}]}


async def test_priority_interrupts_then_starts_a_turn(world: World) -> None:
    m, sid, c = await _lane_app(world, PRIORITY)
    router = m.router(sid)
    writes: list[bytes] = []
    pty = router.session.pty
    original = pty._write_locked

    async def spy(data: bytes) -> None:
        writes.append(bytes(data))
        await original(data)

    setattr(pty, "_write_locked", spy)  # noqa: B010 - instance-level spy
    async with c:
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "slow one", "lane": "idle"})
        first = r.json()["turn_id"]
        await world.until(lambda: router.session.state == "busy")
        writes.clear()
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "first", "lane": "priority"})
        assert r.status_code == 202
        body = r.json()
        assert body["queued"] is False and body["lane"] == "priority"
        assert body["turn_id"] != first and body["state"] == "running"
    # ESC is a write of its own, before the paste of the new turn (then its CR).
    assert writes[0] == b"\x1b"
    assert writes[-2:] == [paste_bytes(f"first\n\n<!-- ccw turn: {body['turn_id']} -->"), b"\r"]
    assert writes.count(b"\x1b") == 1  # the interrupt line came: no second ESC
    turns = {t.turn_id: t for t in m.open_turns(sid).list()}
    assert turns[first].state == "interrupted"
    assert turns[body["turn_id"]].state == "running"
    types = [e["type"] for e in world.events(sid)]
    assert types.index("interrupted") < len(types) - 1 - types[::-1].index("turn_started")


async def test_two_priority_sends_space_their_escs(world: World) -> None:
    m, sid, c = await _lane_app(world, PRIORITY)
    keys = world.session_dir(sid) / "config" / "fake-keys.jsonl"
    async with c:
        await c.post(f"/v1/sessions/{sid}/turns", json={"text": "slow one", "lane": "idle"})
        await world.until(lambda: m.router(sid).session.state == "busy")
        for text in ("first", "second"):
            r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": text, "lane": "priority"})
            assert r.status_code == 202 and r.json()["queued"] is False
    await world.until(lambda: len(jsonl(keys)) >= 2)
    stamps = [_ts(k["ts"]) for k in jsonl(keys)]
    assert len(stamps) == 2 and stamps[1] - stamps[0] >= ESC_SPACING_SECONDS - 0.05
    states = [t.state for t in m.open_turns(sid).list()]
    assert states == ["interrupted", "interrupted", "running"]


async def test_priority_settle_timeout_escalates_then_submits(world: World) -> None:
    # The fake prints and goes idle without a Stop: no interrupt line ever comes.
    scenario = {
        "turns": [
            {"match": "wedge", "steps": [{"print": "working...\n"}]},
            {"match": "urgent", "steps": [{"text": "on it"}]},
        ]
    }
    m, sid, c = await _lane_app(world, scenario, sessions={"interrupt_settle_seconds": 0.4})
    keys = world.session_dir(sid) / "config" / "fake-keys.jsonl"
    async with c:
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "wedge", "lane": "idle"})
        wedged = r.json()["turn_id"]
        await world.until(lambda: m.router(sid).session.ring.contains("working..."))
        started = time.monotonic()
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "urgent", "lane": "priority"})
        assert time.monotonic() - started >= 0.4
        assert r.status_code == 202 and r.json()["queued"] is False
        urgent = r.json()["turn_id"]
    await world.wait_turn(m, sid)
    turns = {t.turn_id: t for t in m.open_turns(sid).list()}
    assert turns[wedged].state == "interrupted"
    assert turns[urgent].state == "completed" and turns[urgent].final_text == "on it"
    assert len(jsonl(keys)) == 2  # ESC, then the second ESC after the settle time


async def test_priority_on_an_idle_session_is_a_plain_turn(world: World) -> None:
    m, sid, c = await _lane_app(world, {})
    keys = world.session_dir(sid) / "config" / "fake-keys.jsonl"
    async with c:
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "hi", "lane": "priority"})
        assert r.status_code == 202
        assert r.json()["queued"] is False and r.json()["lane"] == "priority"
    await world.wait_turn(m, sid)
    assert jsonl(keys) == []
    assert "interrupted" not in [e["type"] for e in world.events(sid)]


async def test_vetoed_priority_does_not_interrupt(world: World, tmp_path) -> None:
    hook = {
        "type": "command",
        "run": "echo no >&2; exit 2",
        "blocking": True,
    }
    m, sid, c = await _lane_app(world, PRIORITY, hooks={"onTurn": [hook]})
    keys = world.session_dir(sid) / "config" / "fake-keys.jsonl"
    router = m.router(sid)
    record = await router.session.submit("slow one")  # straight in: no onTurn for it
    async with c:
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "first", "lane": "priority"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "vetoed"
    assert jsonl(keys) == []
    assert router.session.current_turn is record and record.state == "running"


# ------------------------------------------------------------------ C4: dialog refusal

DIALOG = {
    "turns": [
        {
            "match": "edit",
            "steps": [
                {"text": "Do you want to proceed?\n 1. Yes\n 2. No\n Esc to cancel"},
                {"sleep": 30},
            ],
        }
    ]
}


async def test_open_dialog_refuses_regular_and_priority(world: World) -> None:
    m, sid, c = await _lane_app(world, DIALOG)
    router = m.router(sid)
    session = router.session
    cfg_dir = world.session_dir(sid) / "config"
    async with c:
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "edit it", "lane": "idle"})
        running = r.json()["turn_id"]
        await world.until(session.dialog_open, what="the permission prompt on screen")
        writes: list[bytes] = []
        original = session.pty._write_locked

        async def spy(data: bytes) -> None:
            writes.append(bytes(data))
            await original(data)

        setattr(session.pty, "_write_locked", spy)  # noqa: B010 - instance-level spy
        for lane in ("regular", "priority"):
            r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "go", "lane": lane})
            assert r.status_code == 409, r.text
            assert r.json()["error"]["code"] == "dialog_open"
        assert writes == []  # nothing typed into the dialog, no ESC
        assert jsonl(cfg_dir / "fake-keys.jsonl") == []
        assert session.current_turn is not None and session.current_turn.turn_id == running
        assert "prompt_queued" not in [e["type"] for e in world.events(sid)]
        # A relayed hook (the tool ran: the prompt was answered) closes it.
        session.note_hook()
        assert not session.dialog_open()
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "go", "lane": "regular"})
        assert r.status_code == 202 and r.json()["queued"] is True


async def test_dialog_closed_by_escape_and_own_echo_ignored(harness: Harness) -> None:  # noqa: F811
    session = await harness.start(DIALOG)
    await session.submit("edit it")
    await wait_until(session.dialog_open)
    with pytest.raises(TurnRejected) as exc:
        await session.queue_prompt("x", lane="regular", origin="client")
    assert exc.value.reason == "dialog_open"
    await session.send_escape()  # an ESC closes (declines) the dialog
    assert not session.dialog_open()
    await session.stop(grace=1.0)
    # Text we typed ourselves that quotes a footer is not a dialog.
    session = await harness.start({"turns": [{"match": "q", "steps": [{"sleep": 30}]}]})
    await session.submit("q: what does Esc to cancel mean?")
    await wait_until(lambda: session.ring.contains("Esc to cancel"))
    assert not session.dialog_open()
