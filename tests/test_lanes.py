"""Delivery lanes on `POST /v1/sessions/{id}/turns` (agents-ui-improvements C1-C5)."""

from __future__ import annotations

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
from test_session_turns import Harness, harness  # noqa: F401 - fixture

from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.api.turns import LANES, TurnRequest
from claude_code_wrapper.manager import CreateSessionRequest, InvalidState
from claude_code_wrapper.pty_proc import paste_bytes
from claude_code_wrapper.session import TurnRejected


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
