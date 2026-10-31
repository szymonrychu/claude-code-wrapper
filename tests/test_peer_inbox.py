"""Per-session peer inbox: frames, bounds, rate limit and delivery as new turns (D7, D9, D14)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from conftest import World, turns_of
from test_command_runner import commands_cfg
from test_router import SECRET, SID, Harness, StubSession, webhook

from claude_code_wrapper.manager import CreateSessionRequest
from claude_code_wrapper.metrics import Metrics
from claude_code_wrapper.peer_client import PeerError
from claude_code_wrapper.peer_inbox import (
    InboxItem,
    PeerInbox,
    PutResult,
    frame_message,
    frame_notice,
)
from claude_code_wrapper.turns import TurnRecord

FROM = "a/ses_0123456789ab"
OTHER = "b/ses_ba9876543210"
RECEIVED_KEYS = {"msg_id", "from", "kind", "bytes", "message", "queued"}
DELIVERED_KEYS = {"msg_id", "from", "kind", "turn_id", "result"}


def msg(n: int | str, text: str | None = None, sender: str = FROM, lane: str = "idle") -> InboxItem:
    """A peer message on the idle lane: these tests pin delivery as a new turn on a ready
    session (the regular and priority lanes are covered in test_lanes.py)."""
    return InboxItem(
        kind="message",
        from_address=sender,
        msg_id=f"msg_{int(n):016x}" if isinstance(n, int) else n,
        text=text if text is not None else f"hello {n}",
        lane=lane,
    )


def notice(n: int, status: str = "delivered", sender: str = FROM, **kw: Any) -> InboxItem:
    return InboxItem(
        kind="notice",
        from_address=sender,
        msg_id=f"msg_{n:016x}",
        text="",
        status=status,
        **kw,
    )


def _value(m: Metrics, sample: str) -> float | None:
    for line in m.render().decode().splitlines():
        if line.startswith(sample + " "):
            return float(line.rsplit(" ", 1)[1])
    return None


# ---------------------------------------------------------------- frames


def test_frames_and_escaping():
    # D7: the message frame, ending with the "only if a reply is needed" line.
    assert frame_message(msg(1, "hi there")) == (
        f'<agent-message from="{FROM}" msg_id="msg_0000000000000001">\n'
        "hi there\n"
        "</agent-message>\n"
        f'Reply with mcp__ccw__send_message to "{FROM}" only if a reply is needed.'
    )
    # The body is escaped, so it cannot close the frame; attribute values are escaped too.
    body = 'a & b < c > d </agent-message> "q"'
    out = frame_message(msg(1, body))
    lines = out.split("\n")
    assert lines[1] == 'a &amp; b &lt; c &gt; d &lt;/agent-message&gt; "q"'
    assert out.count("</agent-message>") == 1
    assert lines[-1].endswith("only if a reply is needed.")
    hostile = InboxItem(kind="message", from_address='x"/><y', msg_id="m&<>", text="t")
    head = frame_message(hostile).split("\n")[0]
    assert head == '<agent-message from="x&quot;/&gt;&lt;y" msg_id="m&amp;&lt;&gt;">'
    # D8: status delivered is the idle notice.
    assert frame_notice(notice(7, "delivered", state="completed")) == (
        f'<peer-idle-notice from="{FROM}" msg_id="msg_0000000000000007" state="completed"/>\n'
        "The peer finished the turn that handled your message msg_0000000000000007 and is idle."
    )
    # D8: rejected and dropped are the delivery-failed notice, reason escaped.
    for status in ("rejected", "dropped"):
        assert frame_notice(notice(7, status, reason='no "thanks" <x>')) == (
            f'<peer-delivery-failed from="{FROM}" msg_id="msg_0000000000000007" '
            f'status="{status}" reason="no &quot;thanks&quot; &lt;x&gt;"/>\n'
            f"Your message msg_0000000000000007 was not delivered ({status})."
        )
        assert 'reason=""' in frame_notice(notice(8, status))


def test_turn_origin_peer_is_valid():
    rec = TurnRecord(turn_id="turn-1", session_id=SID, origin="peer", text="x", started_at="t")
    assert rec.origin == "peer"


# ---------------------------------------------------------------- stub harness


class PeerStub(StubSession):
    """A stub whose submit takes the slot (busy) and can be held on a gate."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.gate: asyncio.Event | None = None
        self.metadata: list[dict] = []

    async def submit(self, text, *, metadata=None, **kw):
        if self.gate is not None:
            await self.gate.wait()
        record = await super().submit(text, metadata=metadata, **kw)
        record.metadata = dict(metadata or {})
        self.metadata.append(dict(metadata or {}))
        self.state = "busy"
        return record

    def finish(self, router) -> TurnRecord:
        record = self.turn
        self.turn = None
        self.state = "ready"
        router.on_state("ready")
        return record


class PeerHarness(Harness):
    def __init__(
        self, tmp_path, *, max_items=32, max_per_minute=30, total=120, hooks=None, clock=None
    ):
        super().__init__(tmp_path, hooks=hooks)
        self.session = PeerStub(self.workdir, self.transcript)
        self.router.bind(self.session)  # type: ignore[arg-type]
        self.metrics = Metrics()
        kw = {} if clock is None else {"clock": clock}
        self.inbox = PeerInbox(
            self.router,
            max_items=max_items,
            max_per_minute=max_per_minute,
            max_total_per_minute=total,
            metrics=self.metrics,
            **kw,
        )
        self.router.peer_inbox = self.inbox

    def submits(self) -> list[tuple]:
        return self.calls("submit")

    def delivered(self) -> list[dict]:
        return [e.data for e in self.events("peer_message_delivered")]


@pytest.fixture
async def ph(tmp_path):
    harness = PeerHarness(tmp_path)
    yield harness
    await harness.router.close()


async def until(pred, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


async def test_idle_session_gets_turn(ph):
    item = msg(1, "hi")
    res = ph.inbox.put(item)
    assert res == PutResult(depth=1, duplicate=False)
    await until(lambda: ph.submits())
    (call,) = ph.submits()
    assert call[1] == frame_message(item) and call[2] == "peer"
    assert ph.session.metadata == [
        {"peer": {"from": FROM, "msg_id": item.msg_id, "kind": "message"}}
    ]
    (received,) = [e.data for e in ph.events("peer_message_received")]
    assert received["msg_id"] == item.msg_id and received["queued"] == 1
    (delivered,) = ph.delivered()
    assert delivered == {
        "msg_id": item.msg_id,
        "from": FROM,
        "kind": "message",
        "turn_id": ph.session.turn.turn_id,
        "result": "delivered",
    }
    assert ph.inbox.depth == 0
    assert ph.inbox.delivered_turn(ph.session.turn.turn_id) is item
    assert _value(
        ph.metrics, 'ccw_peer_messages_delivered_total{kind="message",result="delivered"}'
    )


async def test_events_carry_d9_keys(ph):
    ph.inbox.put(msg(1, "hello world"))
    await until(lambda: ph.delivered())
    (received,) = [e.data for e in ph.events("peer_message_received")]
    assert set(received) == RECEIVED_KEYS
    assert received == {
        "msg_id": "msg_0000000000000001",
        "from": FROM,
        "kind": "message",
        "bytes": len(b"hello world"),
        "message": "hello world",
        "queued": 1,
    }
    (delivered,) = ph.delivered()
    assert set(delivered) == DELIVERED_KEYS
    sample = 'ccw_peer_messages_received_total{kind="message",result="accepted"}'
    assert _value(ph.metrics, sample) == 1.0


async def test_events_are_redacted(ph):
    ph.inbox.put(msg(1, f"token {SECRET}"))
    await until(lambda: ph.delivered())
    (received,) = [e.data for e in ph.events("peer_message_received")]
    assert SECRET not in received["message"]


async def test_busy_session_waits_for_ready(ph):
    ph.session.state = "busy"
    ph.session.turn = TurnRecord(
        turn_id="turn-0", session_id=SID, origin="client", text="x", started_at="t"
    )
    ph.inbox.put(msg(1))
    await asyncio.sleep(0.1)
    assert ph.submits() == [] and ph.inbox.depth == 1
    ph.session.finish(ph.router)
    await until(lambda: ph.submits())
    assert ph.inbox.depth == 0


async def test_arrival_order(ph):
    items = [msg(i) for i in (1, 2, 3)]
    for item in items:
        ph.inbox.put(item)
    for n in (1, 2, 3):
        await until(lambda n=n: len(ph.submits()) == n)
        await asyncio.sleep(0.05)
        assert len(ph.submits()) == n  # one turn at a time: the next waits for ready
        ph.session.finish(ph.router)
    assert [c[1] for c in ph.submits()] == [frame_message(i) for i in items]
    assert [d["msg_id"] for d in ph.delivered()] == [i.msg_id for i in items]


class VetoHarness(PeerHarness):
    """onTurn webhook that denies the peer message `msg_0000000000000001`."""

    def _handler(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        peer = body["data"]["metadata"].get("peer", {})
        if peer.get("msg_id") == "msg_0000000000000001":
            self.hook_posts.append(body)
            return httpx.Response(200, json={"decision": "deny", "reason": "not you"})
        return httpx.Response(200, json={})


async def test_veto_of_head_does_not_strand_queue(tmp_path):
    h = VetoHarness(tmp_path, hooks={"onTurn": [webhook()]})
    try:
        items = [msg(i) for i in (1, 2, 3)]
        for item in items:
            h.inbox.put(item)
        await until(lambda: len(h.submits()) == 1)
        h.session.finish(h.router)
        await until(lambda: len(h.submits()) == 2)
        h.session.finish(h.router)
        assert len(h.hook_posts) == 1  # the head was vetoed
        assert [c[1] for c in h.submits()] == [frame_message(i) for i in items[1:]]
        results = [(d["msg_id"], d["result"], d["turn_id"]) for d in h.delivered()]
        assert results[0] == ("msg_0000000000000001", "rejected", None)
        assert [r[1] for r in results[1:]] == ["delivered", "delivered"]
        assert items[0].reason == "not you"
        sample = 'ccw_peer_messages_delivered_total{kind="message",result="rejected"}'
        assert _value(h.metrics, sample) == 1.0
    finally:
        await h.router.close()


async def test_kick_mid_attempt(ph):
    gate = asyncio.Event()
    ph.session.gate = gate
    item = msg(1)
    ph.inbox.put(item)
    await until(lambda: ph.inbox._task is not None)
    await asyncio.sleep(0.02)  # the attempt is held inside submit
    # A client turn takes the slot under the attempt: its submit is rejected, the session
    # stays busy, so only the pending kick brings the inbox back.
    ph.session.reject = "busy"
    ph.session.state = "busy"
    ph.inbox.kick()

    def free(_task):
        ph.session.reject = None
        ph.session.state = "ready"

    ph.inbox._task.add_done_callback(free)
    ph.session.gate = None
    gate.set()
    await until(lambda: ph.submits())
    assert ph.submits()[0][1] == frame_message(item)
    assert ph.inbox.depth == 0


async def test_turn_rejected_keeps_head_until_ready(ph):
    ph.session.reject = "booting"
    ph.inbox.put(msg(1))
    await asyncio.sleep(0.1)
    assert ph.inbox.depth == 1 and ph.delivered() == []
    ph.session.reject = None
    ph.session.state = "ready"
    ph.router.on_state("ready")
    await until(lambda: ph.submits())
    assert ph.inbox.depth == 0


async def test_unexpected_error_keeps_head_queued(ph, monkeypatch, caplog):
    calls = {"n": 0}
    real = ph.router.submit_turn

    async def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return await real(*a, **kw)

    monkeypatch.setattr(ph.router, "submit_turn", flaky)
    ph.inbox.put(msg(1))
    await until(lambda: ph.submits())
    assert calls["n"] == 2 and ph.inbox.depth == 0
    assert "boom" in caplog.text


async def test_not_delivered_while_idle_command_queued(ph, monkeypatch):
    queued = SimpleNamespace(turn_id=None)
    monkeypatch.setattr(ph.router.commands, "pending", lambda: [queued])
    ph.inbox.put(msg(1))
    await asyncio.sleep(0.1)
    assert ph.submits() == []
    monkeypatch.setattr(ph.router.commands, "pending", lambda: [])
    await until(lambda: ph.submits())


async def test_not_delivered_while_runner_active(ph, monkeypatch):
    runner = asyncio.get_running_loop().create_future()
    monkeypatch.setattr(ph.router, "_runner", runner)
    ph.inbox.put(msg(1))
    await asyncio.sleep(0.1)
    assert ph.submits() == []
    runner.set_result(None)  # the runner finished while the session stayed ready
    await until(lambda: ph.submits())


async def test_bounds_rate_limit_and_duplicates(tmp_path):
    now = [1000.0]
    for name in ("full", "rated", "dup"):
        (tmp_path / name).mkdir()
    full = PeerHarness(tmp_path / "full", max_items=2)
    rated = PeerHarness(tmp_path / "rated", max_items=100, clock=lambda: now[0])
    dup = PeerHarness(tmp_path / "dup")
    for h in (full, rated, dup):
        h.session.state = "busy"
    try:
        # A queue already holding max_items raises inbox_full.
        full.inbox.put(msg(1))
        full.inbox.put(msg(2))
        with pytest.raises(PeerError) as err:
            full.inbox.put(msg(3))
        assert err.value.code == "inbox_full"
        assert full.inbox.depth == 2
        sample = 'ccw_peer_messages_received_total{kind="message",result="inbox_full"}'
        assert _value(full.metrics, sample) == 1.0
        assert len(full.events("peer_message_received")) == 2

        # The 31st message in a minute from one sender raises rate_limited.
        for i in range(30):
            rated.inbox.put(msg(i))
        with pytest.raises(PeerError) as err:
            rated.inbox.put(msg(30))
        assert err.value.code == "rate_limited"
        rated.inbox.put(msg(31, sender=OTHER))  # another sender passes
        rated.inbox.put(notice(32))  # notices are never rate limited
        sample = 'ccw_peer_messages_received_total{kind="message",result="rate_limited"}'
        assert _value(rated.metrics, sample) == 1.0
        now[0] += 60.5  # the window slides
        rated.inbox.put(msg(33))
        assert rated.inbox.depth == 33

        # A repeated (from, msg_id, kind) is not queued twice.
        first = dup.inbox.put(msg(1))
        again = dup.inbox.put(msg(1))
        assert first == PutResult(depth=1, duplicate=False)
        assert again == PutResult(depth=1, duplicate=True)
        # Same msg_id from another sender, or as a notice, is a different item.
        assert not dup.inbox.put(msg(1, sender=OTHER)).duplicate
        assert not dup.inbox.put(notice(1)).duplicate
        assert dup.inbox.depth == 3
        assert len(dup.events("peer_message_received")) == 3
        sample = 'ccw_peer_messages_received_total{kind="message",result="duplicate"}'
        assert _value(dup.metrics, sample) == 1.0
    finally:
        for h in (full, rated, dup):
            await h.router.close()


async def test_dedupe_window_is_256(tmp_path):
    h = PeerHarness(tmp_path, max_items=1000, max_per_minute=1000, total=1000)
    h.session.state = "busy"
    try:
        for i in range(257):
            h.inbox.put(msg(i))
        assert h.inbox.put(msg(256)).duplicate
        assert not h.inbox.put(msg(0)).duplicate  # aged out of the last 256
    finally:
        await h.router.close()


async def test_notice_item_framed_as_notice(ph):
    item = notice(9, "dropped")
    ph.inbox.put(item)
    await until(lambda: ph.submits())
    assert ph.submits()[0][1] == frame_notice(item)
    assert ph.session.metadata[0]["peer"]["kind"] == "notice"


async def test_close_drops(ph):
    ph.session.state = "busy"
    items = [msg(1), msg(2)]
    for item in items:
        ph.inbox.put(item)
    dropped = ph.inbox.close()
    assert dropped == items
    assert [(d["msg_id"], d["result"], d["turn_id"]) for d in ph.delivered()] == [
        (i.msg_id, "dropped", None) for i in items
    ]
    sample = 'ccw_peer_messages_delivered_total{kind="message",result="dropped"}'
    assert _value(ph.metrics, sample) == 2.0
    assert ph.inbox.close() == []
    ph.session.state = "ready"
    ph.inbox.kick()
    await asyncio.sleep(0.05)
    assert ph.submits() == []


async def test_router_close_closes_inbox_before_session_closed(ph):
    ph.session.state = "busy"
    ph.inbox.put(msg(1))
    await ph.router.close()
    types = [e.type for e in ph.events()]
    assert types.index("peer_message_delivered") < types.index("session_closed")
    assert ph.delivered()[0]["result"] == "dropped"


async def test_after_turn_forgets_turn_mapping(ph):
    ph.inbox.put(msg(1))
    await until(lambda: ph.submits())
    record = ph.session.turn
    assert ph.inbox.delivered_turn(record.turn_id) is not None
    record.state = "completed"
    ph.session.complete_result = record
    await ph.router.handle_claude_hook("Stop", {})
    assert ph.inbox.delivered_turn(record.turn_id) is None


async def test_emit_event_redacts(ph):
    event = ph.router.emit_event("peer_message_sent", {"x": SECRET})
    assert event.type == "peer_message_sent" and SECRET not in str(event.data)


# ---------------------------------------------------------------- World: real PTY, fake claude


async def start_peer(world: World, scenario: dict | None = None, **sections: Any):
    cfg = world.cfg(scenario, **sections)
    m = world.manager(cfg)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    router = m.router(sid)
    inbox = PeerInbox(router, max_items=32, max_per_minute=30)
    router.peer_inbox = inbox
    return m, sid, router, inbox


def completed(m, sid) -> list:
    return [t for t in turns_of(m, sid) if t.state == "completed"]


async def test_world_idle_session_gets_peer_turn(world):
    m, sid, router, inbox = await start_peer(world)
    item = msg(1, "ping")
    inbox.put(item)
    await world.until(lambda: completed(m, sid), what="peer turn completed")
    (turn,) = turns_of(m, sid)
    assert turn.origin == "peer" and turn.text == frame_message(item)
    assert turn.metadata == {"peer": {"from": FROM, "msg_id": item.msg_id, "kind": "message"}}


async def test_busy_session_waits_for_turn_end(world):
    scenario = {"turns": [{"match": "slow", "steps": [{"sleep": 1.0}, {"text": "done"}]}]}
    m, sid, router, inbox = await start_peer(world, scenario)
    await router.submit_turn("slow")
    item = msg(1, "ping")
    inbox.put(item)
    await asyncio.sleep(0.4)
    assert [t.origin for t in turns_of(m, sid)] == ["client"]  # never typed into a busy PTY
    await world.until(lambda: len(completed(m, sid)) == 2, what="both turns completed")
    client, peer = turns_of(m, sid)
    assert client.origin == "client" and peer.origin == "peer"
    assert client.completed_at <= peer.started_at


async def test_world_three_messages_in_arrival_order(world):
    m, sid, router, inbox = await start_peer(world)
    items = [msg(i, f"note {i}") for i in (1, 2, 3)]
    for item in items:
        inbox.put(item)
    await world.until(lambda: len(completed(m, sid)) == 3, what="three peer turns")
    turns = turns_of(m, sid)
    assert [t.text for t in turns] == [frame_message(i) for i in items]
    assert all(t.origin == "peer" for t in turns)


async def test_interrupt_then_next(world):
    scenario = {"turns": [{"match": "SLOWPEER", "steps": [{"sleep": 5.0}, {"text": "x"}]}]}
    m, sid, router, inbox = await start_peer(world, scenario)
    first, second = msg(1, "SLOWPEER"), msg(2, "quick")
    inbox.put(first)
    inbox.put(second)
    await world.until(
        lambda: router.session.current_turn is not None and router.session.state == "busy",
        what="first peer turn running",
    )
    await asyncio.sleep(0.3)
    await router.interrupt()
    await world.until(lambda: completed(m, sid), what="second peer turn completed")
    a, b = turns_of(m, sid)
    assert a.text == frame_message(first) and a.state == "interrupted"
    assert b.text == frame_message(second) and b.state == "completed"


async def test_idle_compact_races_pop(world):
    # The onTurn hook holds the attempt between the readiness check and the paste until the
    # idle compact has taken the slot; the compact then runs for a while.
    started, go = world.tmp / "hook-started", world.tmp / "hook-go"
    gate = f"touch {started}; while [ ! -f {go} ]; do sleep 0.02; done"
    hooks = {"onTurn": [{"type": "command", "run": gate, "blocking": True}]}
    scenario = {"commands": {"compact": {"delay_seconds": 1.0}}}
    m, sid, router, inbox = await start_peer(
        world, scenario, sessions={"commands": commands_cfg()}, hooks=hooks
    )
    item = msg(1, "after compact")
    inbox.put(item)
    await world.until(started.exists, what="onTurn hook holding the attempt")
    record = router.request_command("compact", origin="idle")
    assert router.session.command_active  # the idle compact took the slot
    go.touch()
    await world.until(lambda: record.state == "running", what="compact running")
    await asyncio.sleep(0.2)
    assert inbox.depth == 1 and turns_of(m, sid) == []  # the head stayed queued
    await world.until(lambda: completed(m, sid), timeout=20, what="peer turn after compact")
    assert record.state == "completed"
    (turn,) = turns_of(m, sid)
    assert turn.text == frame_message(item) and turn.origin == "peer"
    types = [e["type"] for e in world.events(sid)]
    assert types.index("turn_started") > types.index("command_completed")
    assert inbox.depth == 0
