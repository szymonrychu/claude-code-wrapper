"""Delivery lanes on `POST /v1/sessions/{id}/turns` (agents-ui-improvements C1-C5)."""

from __future__ import annotations

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
from claude_code_wrapper.session import TurnRejected
from claude_code_wrapper.transcript import UserPrompt, parse_line
from claude_code_wrapper.turns import LANES


def post(client: TestClient, body: dict[str, Any]):  # noqa: F811
    return client.post(f"/v1/sessions/{SID}/turns", json=body)


def test_lane_values_and_default_regular() -> None:
    assert LANES == ("regular", "interrupt", "idle")
    assert TurnRequest(text="hi").lane == "regular"
    for lane in LANES:
        assert TurnRequest(text="hi", lane=lane).lane == lane


@pytest.mark.parametrize("lane", ["priority", "urgent", "", "IDLE", 1, None])
def test_unknown_lane_is_422(client: TestClient, stub: StubManager, lane: Any) -> None:  # noqa: F811
    r = post(client, {"text": "hi", "lane": lane})
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "invalid_request" and "lane" in body["message"]
    assert stub.router_obj.calls == []


# ------------------------------------------------------------------ C2: regular lane


def message_tag(message_id: str) -> str:
    return f"\n\n<!-- ccw message: {message_id} -->"


@pytest.mark.parametrize(
    ("lane", "key"), [("regular", b"\r"), ("idle", b"\r"), ("interrupt", b"\x1b[13;5u")]
)
async def test_queue_prompt_pastes_into_the_running_turn(
    harness: Harness,  # noqa: F811
    lane: str,
    key: bytes,
) -> None:
    session = await harness.start({"turns": [{"match": "slow", "steps": [{"sleep": 30}]}]})
    record = await session.submit("slow one")
    harness.writes.clear()
    msg = await session.queue_prompt("steer left", lane=lane, origin="client")
    assert msg.message_id.startswith("msg-") and msg.lane == lane
    assert msg.turn_id == record.turn_id and msg.origin == "client" and msg.queued_at
    # The paste with the message marker, then the lane's submit key: Enter, or Ctrl+Enter
    # (CSI-u) on interrupt. No ESC, no turn marker.
    assert harness.writes == [paste_bytes("steer left" + message_tag(msg.message_id)), key]
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


@pytest.mark.parametrize("lane", ["regular", "idle"])
async def test_busy_session_types_regular_and_idle_into_the_running_turn(
    world: World, lane: str
) -> None:
    cfg = world.cfg(SLOW, server={"auth_token": TOKEN})
    m = world.manager(cfg)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    transport = httpx.ASGITransport(app=create_app(m, cfg))
    keys = world.session_dir(sid) / "config" / "fake-keys.jsonl"
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=H) as c:
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "slow one", "lane": "idle"})
        assert r.status_code == 202 and r.json()["queued"] is False
        running = r.json()["turn_id"]
        # Busy: typed into the running turn.
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "also do this", "lane": lane})
        assert r.status_code == 202
        body = r.json()
        assert body["queued"] is True and body["lane"] == lane
        assert body["turn_id"] == running and body["state"] == "running"
        assert body["message_id"].startswith("msg-")
        await world.wait_turn(m, sid)
    assert jsonl(keys) == []  # no ESC
    events = world.events(sid)
    queued = [e["data"] for e in events if e["type"] == "prompt_queued"]
    assert queued == [
        {
            "message_id": body["message_id"],
            "lane": lane,
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
        for lane in ("regular", "interrupt"):
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
    for lane in ("idle", "regular", "interrupt"):
        assert post(client, {"text": "a", "lane": lane}).status_code == 202
    assert stub.router_obj.lanes == ["idle", "regular", "interrupt"]


# ------------------------------------------------------------------ C3: interrupt lane


async def _lane_app(world: World, scenario: dict, **sections: Any):
    cfg = world.cfg(scenario, server={"auth_token": TOKEN}, **sections)
    m = world.manager(cfg)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    transport = httpx.ASGITransport(app=create_app(m, cfg))
    return m, sid, httpx.AsyncClient(transport=transport, base_url="http://t", headers=H)


SLOW30 = {"turns": [{"match": "slow|first|second", "steps": [{"sleep": 30}]}]}

SEND_NOW = {
    "queue_input": True,
    "turns": [
        {"match": "slow", "steps": [{"sleep": 30}, {"text": "done"}]},
        {"match": "urgent", "steps": [{"text": "on it"}]},
    ],
}


async def test_interrupt_lane_hands_the_text_to_the_running_turn_now(world: World) -> None:
    # Ctrl+Enter (chat:sendNow): claude takes the message into the running turn now. No ESC,
    # no new turn: the turn that reads it completes with its one Stop.
    m, sid, c = await _lane_app(world, SEND_NOW)
    keys = world.session_dir(sid) / "config" / "fake-keys.jsonl"
    session = m.router(sid).session
    async with c:
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "slow one", "lane": "idle"})
        running = r.json()["turn_id"]
        await world.until(lambda: session.state == "busy")
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "urgent", "lane": "interrupt"})
        assert r.status_code == 202
        body = r.json()
        assert body["queued"] is True and body["lane"] == "interrupt"
        assert body["turn_id"] == running
    await world.until(
        lambda: m.open_turns(sid).get(running).state == "completed",
    )
    assert [k["key"] for k in jsonl(keys)] == ["ctrl_enter"]
    assert [t.turn_id for t in m.open_turns(sid).list()] == [running]
    delivered = [e["data"] for e in world.events(sid) if e["type"] == "prompt_delivered"]
    assert delivered == [
        {
            "message_id": body["message_id"],
            "lane": "interrupt",
            "turn_id": running,
            "new_turn": False,
        }
    ]
    assert "interrupted" not in [e["type"] for e in world.events(sid)]


@pytest.mark.parametrize("lane", ["interrupt", "regular", "idle"])
async def test_every_lane_starts_a_turn_on_an_idle_session(world: World, lane: str) -> None:
    m, sid, c = await _lane_app(world, {})
    keys = world.session_dir(sid) / "config" / "fake-keys.jsonl"
    async with c:
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "hi", "lane": lane})
        assert r.status_code == 202
        assert r.json()["queued"] is False and r.json()["lane"] == lane
    await world.wait_turn(m, sid)
    assert jsonl(keys) == []
    assert "interrupted" not in [e["type"] for e in world.events(sid)]


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


async def test_open_permission_prompt_refuses_every_lane(world: World) -> None:
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
        for lane in ("regular", "interrupt", "idle"):
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


OTHER_DIALOG = {
    "queue_input": True,
    "turns": [
        {
            "match": "pick",
            "steps": [{"text": "Pick a colour\n 1. Red\n 2. Blue\n Esc to cancel"}, {"sleep": 30}],
        }
    ],
}


@pytest.mark.parametrize(("lane", "status"), [("regular", 202), ("interrupt", 202), ("idle", 409)])
async def test_other_dialog_footer_holds_only_the_idle_lane(
    world: World, lane: str, status: int
) -> None:
    # A footer that is not a permission prompt (or a stale one with no hook after it) holds
    # only `idle`; `regular` and `interrupt` are typed at once.
    m, sid, c = await _lane_app(world, OTHER_DIALOG)
    session = m.router(sid).session
    async with c:
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "pick one", "lane": "idle"})
        running = r.json()["turn_id"]
        await world.until(session.dialog_open, what="the dialog footer on screen")
        assert not session.permission_prompt_open()
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "go", "lane": lane})
        assert r.status_code == status, r.text
        if status == 202:
            assert r.json()["queued"] is True and r.json()["turn_id"] == running
        else:
            assert r.json()["error"]["code"] == "dialog_open"


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


@pytest.mark.parametrize("lane", ["regular", "interrupt", "idle"])
async def test_peer_message_is_typed_into_the_running_turn(world: World, lane: str) -> None:
    outcomes: list[tuple[str, str, str | None]] = []

    async def on_outcome(item, status, record) -> None:
        outcomes.append((item.msg_id, status, record.turn_id if record else None))

    m, sid, router, inbox = await start_peer(world, QUEUE)
    inbox._on_outcome = on_outcome
    running = await router.submit_turn("slow one")
    item = peer_msg(1, "ping", lane, notify=True)
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


async def test_peer_idle_message_held_by_a_dialog_lets_a_regular_one_pass(world: World) -> None:
    m, sid, router, inbox = await start_peer(world, OTHER_DIALOG)
    running = await router.submit_turn("pick one")
    await world.until(router.session.dialog_open, what="the dialog footer on screen")
    held, passing = peer_msg(1, "later", "idle"), peer_msg(2, "now", "regular")
    inbox.put(held)
    inbox.put(passing)
    await world.until(lambda: inbox.depth == 1, what="the regular message left the inbox")
    assert inbox._items[0] is held
    (queued,) = [e["data"] for e in world.events(sid) if e["type"] == "prompt_queued"]
    assert queued["lane"] == "regular" and queued["turn_id"] == running.turn_id


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
    for args in ({"lane": "interrupt"}, {}, {"lane": "idle"}):
        call = _call("send_message", {"to": "b/s2", "message": "hi", **args})
        reply = await handle_message(FakeRouter(), call, peers)
        assert reply["result"]["isError"] is False
    assert peers.lanes == ["interrupt", "regular", "idle"]
    call = _call("send_message", {"to": "b/s2", "message": "hi", "lane": "priority"})
    reply = await handle_message(FakeRouter(), call, peers)
    assert reply["result"]["isError"] is True
    assert reply["result"]["content"][0]["text"].startswith("invalid arguments")
    assert "lane" in reply["result"]["content"][0]["text"]
    assert len(peers.lanes) == 3


# ------------------------------------------------------------------ review fixes


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

    m, sid, router, inbox = await start_peer(world, SLOW30)
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
