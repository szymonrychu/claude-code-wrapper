"""Delivery lanes on `POST /v1/sessions/{id}/turns` (agents-ui-improvements C1-C5)."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import World, jsonl
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_api_turns import (  # noqa: F401 - fixtures
    SID,
    TOKEN,
    H,
    StubManager,
    _live_app,
    client,
    stub,
)
from test_peer_inbox import FROM, start_peer
from test_session_turns import Harness, harness, wait_until  # noqa: F401 - fixture

from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.api.turns import TurnRequest
from claude_code_wrapper.manager import CreateSessionRequest, InvalidState
from claude_code_wrapper.peer_client import MessageBody
from claude_code_wrapper.peer_inbox import InboxItem
from claude_code_wrapper.pty_proc import paste_bytes
from claude_code_wrapper.session import ESC_SPACING_SECONDS, TurnRejected
from claude_code_wrapper.transcript import UserPrompt, parse_line
from claude_code_wrapper.turns import LANES


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
    session = m.router(sid).session
    escs: list[float] = []  # when the wrapper wrote each ESC (the fake's read time can lag)
    original = session.pty._write_locked

    async def spy(data: bytes) -> None:
        if data == b"\x1b":
            escs.append(time.monotonic())
        await original(data)

    setattr(session.pty, "_write_locked", spy)  # noqa: B010 - instance-level spy
    async with c:
        await c.post(f"/v1/sessions/{sid}/turns", json={"text": "slow one", "lane": "idle"})
        await world.until(lambda: session.state == "busy")
        for text in ("first", "second"):
            r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": text, "lane": "priority"})
            assert r.status_code == 202 and r.json()["queued"] is False
    await world.until(lambda: len(jsonl(keys)) >= 2)
    assert len(escs) == 2 and escs[1] - escs[0] >= ESC_SPACING_SECONDS
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
    assert [t.turn_id for t in m.open_turns(sid).list()] == [record.turn_id]  # no pending left


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


# ------------------------------------------------------------------ C5: turn accounting

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts"


def test_fixture_mid_turn_queued_message_is_a_marked_prompt() -> None:
    events = [
        ev
        for line in (FIXTURES / "queued_message.jsonl").read_text().splitlines()
        for ev in parse_line(line)
    ]
    prompts = [ev for ev in events if isinstance(ev, UserPrompt)]
    assert [(p.turn_marker, p.message_id) for p in prompts] == [
        ("turn-1kq0lanes001", None),
        (None, "msg-1kq0lanes002"),  # absorbed mid-turn: no turn marker, the message id
    ]
    assert "also count them" in prompts[1].text


QUEUE = {
    "queue_input": True,
    "turns": [
        {
            "match": "slow",
            "steps": [
                {"sleep": 1.5},
                {"tool": "Bash", "input": {"command": "ls"}},
                {"text": "done"},
            ],
        },
        {"match": "late", "steps": [{"text": "working"}, {"sleep": 1.5}]},
        {"match": "steer", "steps": [{"text": "steered"}]},
    ],
}


async def test_mid_turn_message_is_delivered_into_the_running_turn(world: World) -> None:
    m, sid, c = await _lane_app(world, QUEUE)
    async with c:
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "slow one", "lane": "idle"})
        running = r.json()["turn_id"]
        r = await c.post(
            f"/v1/sessions/{sid}/turns", json={"text": "steer this", "message_id": "m-1"}
        )
        assert r.status_code == 202 and r.json()["queued"] is True
        assert r.json()["message_id"] == "m-1"  # round-trips
    await world.until(lambda: m.open_turns(sid).get(running).state != "running")
    turns = m.open_turns(sid).list()
    assert [(t.turn_id, t.state, t.final_text) for t in turns] == [(running, "completed", "done")]
    events = world.events(sid)
    delivered = [e["data"] for e in events if e["type"] == "prompt_delivered"]
    assert delivered == [
        {"message_id": "m-1", "lane": "regular", "turn_id": running, "new_turn": False}
    ]
    assert [e["type"] for e in events].count("turn_completed") == 1  # one Stop, one turn
    queued_at = [e["type"] for e in events].index("prompt_queued")
    assert queued_at < [e["type"] for e in events].index("prompt_delivered")
    assert m.router(sid).session.state == "ready"


async def test_message_absorbed_after_the_stop_becomes_a_new_turn(world: World) -> None:
    m, sid, c = await _lane_app(world, QUEUE)
    async with c:
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "late one", "lane": "idle"})
        first = r.json()["turn_id"]
        await world.until(lambda: m.router(sid).session.ring.contains("working"))
        r = await c.post(
            f"/v1/sessions/{sid}/turns",
            json={"text": "steer it", "callbackUrl": f"{world.url}/cb"},
        )
        assert r.status_code == 202 and r.json()["queued"] is True
        message_id = r.json()["message_id"]
    await world.until(lambda: len(m.open_turns(sid).list()) == 2, what="the adopted turn")
    await world.wait_turn(m, sid)
    await world.until(lambda: m.router(sid).session.state == "ready")
    first_rec, second = m.open_turns(sid).list()
    assert first_rec.turn_id == first and first_rec.state == "completed"
    assert second.state == "completed" and second.final_text == "steered"
    assert second.origin == "client" and second.text == "steer it"
    assert second.callback_url == f"{world.url}/cb"
    events = world.events(sid)
    delivered = [e["data"] for e in events if e["type"] == "prompt_delivered"]
    assert delivered == [
        {"message_id": message_id, "lane": "regular", "turn_id": second.turn_id, "new_turn": True}
    ]
    started = [e["data"]["turn_id"] for e in events if e["type"] == "turn_started"]
    assert started == [first, second.turn_id]
    completed = [e["data"]["record"]["turn_id"] for e in events if e["type"] == "turn_completed"]
    assert completed == [first, second.turn_id]  # each turn exactly once
    await world.until(lambda: world.callbacks, what="the adopted turn's callback")
    assert world.callbacks[-1]["data"]["record"]["turn_id"] == second.turn_id


async def test_unknown_message_marker_is_a_plain_prompt(world: World) -> None:
    m, sid, c = await _lane_app(world, {})
    router = m.router(sid)
    await router._on_transcript_events([UserPrompt(text="x", message_id="msg-nope")])
    assert router.session.state == "ready" and router.session.current_turn is None
    assert "prompt_delivered" not in [e["type"] for e in world.events(sid)]
    await c.aclose()


# ------------------------------------------------------------------ C6: peer inbox lanes


def test_peer_message_default_lane_is_regular() -> None:
    item = InboxItem(kind="message", from_address=FROM, msg_id="msg_1", text="hi")
    assert item.lane == "regular"
    body = MessageBody.model_validate(
        {"from": FROM, "to_session": SID, "message": "hi", "msg_id": "msg_0000000000000001"}
    )
    assert body.lane == "regular"
    with pytest.raises(ValidationError):
        MessageBody.model_validate(
            {
                "from": FROM,
                "to_session": SID,
                "message": "hi",
                "msg_id": "msg_0000000000000001",
                "lane": "urgent",
            }
        )


def peer_msg(n: int, text: str, lane: str, notify: bool = False) -> InboxItem:
    return InboxItem(
        kind="message",
        from_address=FROM,
        msg_id=f"msg_{n:016x}",
        text=text,
        lane=lane,
        notify_when_idle=notify,
    )


async def test_peer_regular_lane_types_into_the_running_turn(world: World) -> None:
    outcomes: list[tuple[str, str, str | None]] = []

    async def on_outcome(item, status, record) -> None:
        outcomes.append((item.msg_id, status, record.turn_id if record else None))

    m, sid, router, inbox = await start_peer(world, QUEUE)
    inbox._on_outcome = on_outcome
    running = await router.submit_turn("slow one")
    item = peer_msg(1, "ping", "regular", notify=True)
    inbox.put(item)
    await world.until(lambda: inbox.depth == 0, what="the message left the inbox")
    await world.until(lambda: m.open_turns(sid).get(running.turn_id).state == "completed")
    assert [t.turn_id for t in m.open_turns(sid).list()] == [running.turn_id]  # no new turn
    events = world.events(sid)
    (queued,) = [e["data"] for e in events if e["type"] == "prompt_queued"]
    assert queued["origin"] == "peer" and queued["turn_id"] == running.turn_id
    (delivered,) = [e["data"] for e in events if e["type"] == "peer_message_delivered"]
    assert delivered["turn_id"] == running.turn_id and delivered["result"] == "delivered"
    await world.until(lambda: outcomes, what="the notify_when_idle outcome")
    assert outcomes == [(item.msg_id, "delivered", running.turn_id)]


async def test_peer_idle_lane_waits_for_the_turn_end(world: World) -> None:
    m, sid, router, inbox = await start_peer(world, QUEUE)
    running = await router.submit_turn("slow one")
    inbox.put(peer_msg(1, "steer idle", "idle"))
    await asyncio.sleep(0.4)
    assert inbox.depth == 1
    assert "prompt_queued" not in [e["type"] for e in world.events(sid)]
    await world.until(
        lambda: [t.state for t in m.open_turns(sid).list()][1:] == ["running"],
        what="the peer turn",
    )
    first, peer = m.open_turns(sid).list()
    assert first.turn_id == running.turn_id and peer.origin == "peer"


async def test_peer_priority_lane_interrupts(world: World) -> None:
    m, sid, router, inbox = await start_peer(world, PRIORITY)
    running = await router.submit_turn("slow one")
    await world.until(lambda: router.session.state == "busy")
    inbox.put(peer_msg(1, "steer now", "priority"))
    await world.until(
        lambda: [t.state for t in m.open_turns(sid).list()][1:] == ["running"],
        what="the peer turn",
    )
    first, peer = m.open_turns(sid).list()
    assert first.turn_id == running.turn_id and first.state == "interrupted"
    assert peer.origin == "peer" and "steer now" in peer.text


async def test_peer_regular_lane_on_idle_session_is_a_turn(world: World) -> None:
    m, sid, router, inbox = await start_peer(world, {})
    inbox.put(peer_msg(1, "hello", "regular"))
    await world.until(lambda: m.open_turns(sid).list(), what="the peer turn")
    (turn,) = m.open_turns(sid).list()
    assert turn.origin == "peer"


async def test_send_message_tool_takes_a_lane() -> None:
    from test_mcp_peers import FakePeers, FakeRouter, _call

    from claude_code_wrapper.mcp_server import handle_message

    peers = FakePeers()
    for args in ({"lane": "priority"}, {}, {"lane": "idle"}):
        call = _call("send_message", {"to": "b/s2", "message": "hi", **args})
        reply = await handle_message(FakeRouter(), call, peers)
        assert reply["result"]["isError"] is False
    assert peers.lanes == ["priority", "regular", "idle"]
    call = _call("send_message", {"to": "b/s2", "message": "hi", "lane": "fast"})
    reply = await handle_message(FakeRouter(), call, peers)
    assert reply["result"]["isError"] is True
    assert reply["result"]["content"][0]["text"].startswith("invalid arguments")
    assert "lane" in reply["result"]["content"][0]["text"]
    assert len(peers.lanes) == 3


# ------------------------------------------------------------------ review fixes

FLUSH = {
    "queue_input": True,
    "turns": [
        {"match": "slow|steer", "steps": [{"sleep": 30}]},
        {"match": "urgent", "steps": [{"text": "on it"}]},
    ],
}


async def test_priority_accounts_the_flushed_queue_as_its_own_turn(world: World) -> None:
    # ESC makes claude send the still-queued regular message as a turn of its own: that turn is
    # adopted and interrupted in turn, and only then does the priority message start its turn.
    m, sid, c = await _lane_app(world, FLUSH, sessions={"interrupt_settle_seconds": 3})
    async with c:
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "slow one", "lane": "idle"})
        slow = r.json()["turn_id"]
        await world.until(lambda: m.router(sid).session.state == "busy")
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "steer x"})
        assert r.json()["queued"] is True
        steer_id = r.json()["message_id"]
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "urgent", "lane": "priority"})
        assert r.status_code == 202, " ".join(e["type"] for e in world.events(sid))
        assert r.json()["queued"] is False
        urgent = r.json()["turn_id"]
    await world.until(lambda: m.open_turns(sid).get(urgent).state == "completed")
    turns = m.open_turns(sid).list()
    assert [t.turn_id for t in turns][0] == slow and turns[-1].turn_id == urgent
    assert len(turns) == 3
    flushed = turns[1]
    assert flushed.text == "steer x" and flushed.origin == "client"
    assert [t.state for t in turns] == ["interrupted", "interrupted", "completed"]
    assert turns[2].final_text == "on it"
    delivered = [e["data"] for e in world.events(sid) if e["type"] == "prompt_delivered"]
    assert delivered == [
        {"message_id": steer_id, "lane": "regular", "turn_id": flushed.turn_id, "new_turn": True}
    ]


async def test_priority_keeps_the_slot_between_interrupt_and_submit(world: World) -> None:
    # onInterrupt holds the priority delivery after the running turn ended; a turn submitted in
    # that window must not take the slot the priority message is about to use.
    hook = {"type": "command", "run": "sleep 1", "blocking": True}
    scenario = {
        "turns": [
            {"match": "slow", "steps": [{"sleep": 30}]},
            {"match": "urgent|sneak", "steps": [{"text": "ok"}]},
        ]
    }
    m, sid, c = await _lane_app(world, scenario, hooks={"onInterrupt": [hook]})
    router = m.router(sid)
    async with c:
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "slow one", "lane": "idle"})
        slow = r.json()["turn_id"]
        await world.until(lambda: router.session.state == "busy")
        url = f"/v1/sessions/{sid}/turns"
        priority = asyncio.ensure_future(c.post(url, json={"text": "urgent", "lane": "priority"}))
        await world.until(lambda: m.open_turns(sid).get(slow).state == "interrupted")
        assert router.session.state == "ready"  # the slot is free, but reserved
        sneak = await c.post(url, json={"text": "sneak", "lane": "idle"})
        assert sneak.status_code == 409 and sneak.json()["error"]["code"] == "busy"
        sneak = await c.post(url, json={"text": "sneak"})
        assert sneak.status_code == 409 and sneak.json()["error"]["code"] == "busy"
        r = await priority
        assert r.status_code == 202 and r.json()["queued"] is False
    await world.wait_turn(m, sid)
    assert [t.text for t in m.open_turns(sid).list()] == ["slow one", "urgent"]
    assert not router.session.reserved


async def test_undelivered_messages_are_dropped(harness: Harness) -> None:  # noqa: F811
    dropped: list[tuple[str, str]] = []
    harness.listener.on_prompt_dropped = lambda m, reason: dropped.append((m.message_id, reason))
    scenario = {"turns": [{"match": "slow", "steps": [{"sleep": 1.0}, {"text": "done"}]}]}
    session = await harness.start(scenario)
    first = await session.submit("slow one")
    # Without queue_input the fake drops what is typed while busy: no marker ever comes.
    lost = await session.queue_prompt("lost", lane="regular", origin="client")
    stop = await harness.next_stop()
    session.complete(stop, marker_turn_id=first.turn_id)
    assert dropped == []  # claude may still send it after the turn it was typed into
    second = await session.submit("slow two")
    session.complete(await harness.next_stop(2), marker_turn_id=second.turn_id)
    assert dropped == [(lost.message_id, "not_delivered")]
    assert session.undelivered(first.turn_id) == []
    # A stop drops what is still queued in the running turn.
    third = await session.submit("slow three")
    gone = await session.queue_prompt("gone", lane="regular", origin="client")
    assert third.state == "running"
    await session.stop(grace=1.0)
    assert dropped[-1] == (gone.message_id, "session_stopped")
    assert len(dropped) == 2


async def test_prompt_dropped_event_and_peer_outcome(world: World) -> None:
    outcomes: list[tuple[str, str]] = []

    async def on_outcome(item, status, record) -> None:
        outcomes.append((item.msg_id, status))

    m, sid, router, inbox = await start_peer(world, PRIORITY)
    inbox._on_outcome = on_outcome
    await router.submit_turn("slow one")
    item = peer_msg(1, "ping", "regular")
    inbox.put(item)
    await world.until(lambda: inbox.depth == 0)
    await router.session.stop(grace=1.0)
    (ev,) = [e["data"] for e in world.events(sid) if e["type"] == "prompt_dropped"]
    assert ev["reason"] == "session_stopped" and ev["lane"] == "regular"
    await world.until(lambda: outcomes, what="the dropped outcome")
    assert outcomes == [(item.msg_id, "dropped")]


async def test_nothing_is_typed_into_a_turn_a_priority_delivery_interrupts(world: World) -> None:
    # The ESC brings no interrupt line, so the turn stays running for the settle time; a regular
    # message then must not be typed into it (it would miss the priority's flushed-queue wait).
    scenario = {
        "turns": [
            {"match": "wedge", "steps": [{"print": "working...\n"}]},
            {"match": "urgent", "steps": [{"text": "on it"}]},
        ]
    }
    m, sid, c = await _lane_app(world, scenario, sessions={"interrupt_settle_seconds": 1.0})
    session = m.router(sid).session
    url = f"/v1/sessions/{sid}/turns"
    async with c:
        r = await c.post(url, json={"text": "wedge", "lane": "idle"})
        wedged = r.json()["turn_id"]
        await world.until(lambda: session.ring.contains("working..."))
        priority = asyncio.ensure_future(c.post(url, json={"text": "urgent", "lane": "priority"}))
        await world.until(lambda: session.reserved)
        assert session.current_turn is not None and session.current_turn.turn_id == wedged
        late = await c.post(url, json={"text": "late steer"})
        assert late.status_code == 409 and late.json()["error"]["code"] == "busy"
        r = await priority
        assert r.status_code == 202
    assert "prompt_queued" not in [e["type"] for e in world.events(sid)]


# ------------------------------------------------------------------ priority double delivery

WEDGE = {
    "turns": [
        {"match": "wedge", "steps": [{"print": "working...\n"}]},
        {"match": "urgent", "steps": [{"text": "on it"}]},
    ]
}


async def test_priority_turn_is_recorded_pending_while_the_interrupt_is_delayed(
    world: World,
) -> None:
    # The interrupt waits longer than a client's send timeout (no interrupt line comes): the
    # turn the delivery will start is listed at once, `pending`, with its metadata, so a client
    # that gave up can find it by its metadata instead of concluding it never arrived.
    m, sid, c = await _lane_app(world, WEDGE, sessions={"interrupt_settle_seconds": 1.5})
    session = m.router(sid).session
    url = f"/v1/sessions/{sid}/turns"
    meta = {"orch_message_id": 524}
    async with c:
        r = await c.post(url, json={"text": "wedge", "lane": "idle"})
        await world.until(lambda: session.ring.contains("working..."))
        body = {"text": "urgent", "lane": "priority", "metadata": meta, "message_id": "orch-524"}
        priority = asyncio.ensure_future(c.post(url, json=body))
        await world.until(lambda: session.reserved)
        with pytest.raises(TimeoutError):  # a client timeout shorter than the interrupt
            await asyncio.wait_for(asyncio.shield(priority), 0.3)
        listed = (await c.get(url)).json()["turns"]
        assert [t["state"] for t in listed] == ["running", "pending"]
        pending_id = listed[-1]["turn_id"]
        record = (await c.get(f"{url}/{pending_id}")).json()
        assert record["state"] == "pending" and record["metadata"] == meta
        assert record["text"] == "urgent"
        r = await priority
        assert r.status_code == 202 and r.json()["turn_id"] == pending_id
        assert r.json()["state"] == "running"
    await world.until(lambda: m.open_turns(sid).get(pending_id).state == "completed")
    states = [t.state for t in m.open_turns(sid).list()]
    assert states == ["interrupted", "completed"]


async def test_retried_priority_with_the_same_message_id_delivers_once(world: World) -> None:
    m, sid, c = await _lane_app(world, WEDGE, sessions={"interrupt_settle_seconds": 1.0})
    session = m.router(sid).session
    keys = world.session_dir(sid) / "config" / "fake-keys.jsonl"
    url = f"/v1/sessions/{sid}/turns"
    body = {"text": "urgent", "lane": "priority", "message_id": "orch-7"}
    async with c:
        await c.post(url, json={"text": "wedge", "lane": "idle"})
        await world.until(lambda: session.ring.contains("working..."))
        first = asyncio.ensure_future(c.post(url, json=body))
        await world.until(lambda: session.reserved)
        # A retry while the first is still interrupting: the same turn, still pending.
        retry = await c.post(url, json=body)
        assert retry.status_code == 202
        assert retry.json()["state"] == "pending" and retry.json()["duplicate"] is True
        r = await first
        assert r.status_code == 202 and r.json()["turn_id"] == retry.json()["turn_id"]
        turn_id = r.json()["turn_id"]
        await world.until(lambda: m.open_turns(sid).get(turn_id).state == "completed")
        escs = len(jsonl(keys))
        # A retry after the turn ran: the same turn again, nothing typed, nothing interrupted.
        again = await c.post(url, json=body)
        assert again.status_code == 202 and again.json()["turn_id"] == turn_id
        assert again.json()["state"] == "completed" and again.json()["duplicate"] is True
    assert len(jsonl(keys)) == escs
    assert [t.text for t in m.open_turns(sid).list()] == ["wedge", "urgent"]
    started = [e for e in world.events(sid) if e["type"] == "turn_started"]
    assert [e["data"]["text"] for e in started] == ["wedge", "urgent"]
