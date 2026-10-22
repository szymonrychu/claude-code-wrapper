"""Peer inbox delivery outcomes: the notice hook, run off the Stop hook path (D8)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest
from conftest import World, turns_of
from test_peer_inbox import PeerHarness, VetoHarness, msg, notice, until
from test_router import SECRET, webhook

from claude_code_wrapper.manager import CreateSessionRequest
from claude_code_wrapper.peer_inbox import InboxItem, PeerInbox
from claude_code_wrapper.turns import TurnRecord

Outcome = tuple[InboxItem, str, "TurnRecord | None"]


class Recorder:
    """An `on_outcome` that records each call, optionally blocking or raising first."""

    def __init__(self, *, block: float = 0.0, fail: str | None = None) -> None:
        self.calls: list[Outcome] = []
        self.started: list[Outcome] = []
        self.cancelled = 0
        self.block = block
        self.fail = fail

    async def __call__(self, item: InboxItem, status: str, record: TurnRecord | None) -> None:
        self.started.append((item, status, record))
        if self.block:
            try:
                await asyncio.sleep(self.block)
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        if self.fail is not None:
            raise RuntimeError(self.fail)
        self.calls.append((item, status, record))


def wish(n: int, text: str | None = None) -> InboxItem:
    item = msg(n, text)
    item.notify_when_idle = True
    return item


class OutcomeHarness(PeerHarness):
    def __init__(self, tmp_path, on_outcome, **kw: Any) -> None:
        super().__init__(tmp_path, **kw)
        self.inbox = PeerInbox(
            self.router,
            max_items=32,
            max_per_minute=30,
            metrics=self.metrics,
            on_outcome=on_outcome,
        )
        self.router.peer_inbox = self.inbox

    async def stop(self, state: str = "completed") -> TurnRecord:
        """Finish the running stub turn through the Stop relay."""
        record = self.session.turn
        record.state = state
        self.session.complete_result = record
        await self.router.handle_claude_hook("Stop", {})
        self.session.finish(self.router)
        return record


@pytest.fixture
async def rec():
    return Recorder()


@pytest.fixture
async def oh(tmp_path, rec):
    harness = OutcomeHarness(tmp_path, rec)
    yield harness
    await harness.inbox.drain(1.0)
    await harness.router.close()


# ---------------------------------------------------------------- delivered


async def test_delivered_outcome_only_with_notify(oh, rec):
    # A plain message: its handling turn completes without any outcome.
    oh.inbox.put(msg(1))
    await until(lambda: oh.submits())
    await oh.stop()
    await asyncio.sleep(0.05)
    assert rec.started == []
    # A notify_when_idle message: exactly one `delivered` outcome, after its turn ends,
    # carrying that turn's record and state.
    item = wish(2)
    oh.inbox.put(item)
    await until(lambda: len(oh.submits()) == 2)
    await asyncio.sleep(0.05)
    assert rec.calls == []  # not before the handling turn ends
    record = await oh.stop()
    await until(lambda: rec.calls)
    await oh.inbox.drain(1.0)
    await asyncio.sleep(0.05)
    assert rec.calls == [(item, "delivered", record)]
    assert rec.calls[0][2].state == "completed"


async def check_notices_never_produce_outcomes(tmp_path, rec: Recorder) -> None:
    h = VetoHarness(tmp_path, hooks={"onTurn": [webhook()]})
    h.inbox = PeerInbox(h.router, max_items=32, max_per_minute=30, on_outcome=rec)
    h.router.peer_inbox = h.inbox
    try:
        # A vetoed notice (msg_id 1 is denied), a delivered notice that asked for an idle
        # notice, and a queued notice dropped on close: none produce an outcome.
        h.inbox.put(notice(1, "dropped"))
        delivered = notice(2, "delivered", state="completed")
        delivered.notify_when_idle = True
        h.inbox.put(delivered)
        await until(lambda: h.submits())
        record = h.session.turn
        record.state = "completed"
        h.session.complete_result = record
        await h.router.handle_claude_hook("Stop", {})
        h.session.finish(h.router)
        h.session.state = "busy"
        h.inbox.put(notice(3, "rejected"))
        assert [i.msg_id for i in h.inbox.close()] == ["msg_0000000000000003"]
        results = [d["result"] for d in h.delivered()]
        assert results == ["rejected", "delivered", "dropped"]
        await h.inbox.drain(1.0)
        await asyncio.sleep(0.05)
        assert rec.started == []
    finally:
        await h.router.close()


# ---------------------------------------------------------------- rejected and dropped


async def test_rejected_always_notifies(tmp_path, rec):
    h = VetoHarness(tmp_path, hooks={"onTurn": [webhook()]})
    h.inbox = PeerInbox(h.router, max_items=32, max_per_minute=30, on_outcome=rec)
    h.router.peer_inbox = h.inbox
    try:
        vetoed = msg(1)  # rejected always notifies, notify_when_idle or not
        h.inbox.put(vetoed)
        await until(lambda: rec.calls)
        assert rec.calls == [(vetoed, "rejected", None)]
        assert rec.calls[0][0].reason == "not you"
    finally:
        await h.router.close()


async def test_dropped_always_notifies(oh, rec, tmp_path):
    oh.session.state = "busy"
    items = [msg(1), wish(2), msg(3)]  # dropped always notifies, notify_when_idle or not
    for item in items:
        oh.inbox.put(item)
    oh.inbox.put(notice(4))  # a dropped notice produces no outcome
    assert len(oh.inbox.close()) == 4
    await until(lambda: len(rec.calls) == 3)
    await oh.inbox.drain(1.0)
    await asyncio.sleep(0.05)
    assert rec.calls == [(i, "dropped", None) for i in items]
    # Notices never produce outcomes, whatever their result.
    (tmp_path / "notices").mkdir()
    await check_notices_never_produce_outcomes(tmp_path / "notices", Recorder())


# ---------------------------------------------------------------- off the Stop path


async def blocked_outcome(h: OutcomeHarness, slow: Recorder) -> TurnRecord:
    """Deliver a notify_when_idle message and end its turn through the Stop relay, timed."""
    h.inbox.put(wish(1))
    await until(lambda: h.submits())
    record = h.session.turn
    record.state = "completed"
    h.session.complete_result = record
    t0 = time.monotonic()
    answer = await asyncio.wait_for(h.router.handle_claude_hook("Stop", {}), 1.0)
    assert time.monotonic() - t0 < 1.0
    assert answer["exit_code"] == 0
    await until(lambda: slow.started)  # the outcome is running, blocked on the sender
    assert slow.calls == []
    return record


async def test_slow_sender_does_not_delay_stop(tmp_path):
    slow = Recorder(block=10.0)
    h = OutcomeHarness(tmp_path, slow)
    try:
        record = await blocked_outcome(h, slow)
        (done,) = [e.data["record"] for e in h.events("turn_completed")]
        assert done["turn_id"] == record.turn_id and done["state"] == "completed"
        h.session.finish(h.router)
        assert h.session.state == "ready"
    finally:
        await h.inbox.drain(0.2)
        await h.router.close()


async def test_drain_bounded(tmp_path):
    slow = Recorder(block=10.0)
    h = OutcomeHarness(tmp_path, slow)
    try:
        await blocked_outcome(h, slow)
        t0 = time.monotonic()
        await h.inbox.drain(0.2)
        elapsed = time.monotonic() - t0
        assert 0.15 <= elapsed < 0.6
        assert slow.cancelled == 1
        t0 = time.monotonic()
        await h.inbox.drain(0.2)  # nothing outstanding: returns at once
        assert time.monotonic() - t0 < 0.1
        # A fast outcome finishes inside the window: drain waits for it, cancels nothing.
        fast = Recorder(block=0.05)
        (tmp_path / "fast").mkdir()
        h2 = OutcomeHarness(tmp_path / "fast", fast)
        try:
            await blocked_outcome(h2, fast)
            await h2.inbox.drain(1.0)
            assert fast.cancelled == 0 and len(fast.calls) == 1
        finally:
            await h2.router.close()
    finally:
        await h.router.close()


async def test_blocking_outcome_does_not_delay_delivery_or_close(tmp_path):
    slow = Recorder(block=10.0)
    h = VetoHarness(tmp_path, hooks={"onTurn": [webhook()]})
    h.inbox = PeerInbox(h.router, max_items=32, max_per_minute=30, on_outcome=slow)
    h.router.peer_inbox = h.inbox
    try:
        h.inbox.put(msg(1))  # vetoed: its rejected outcome blocks
        h.inbox.put(msg(2))
        await until(lambda: h.submits())  # the next item is delivered meanwhile
        assert len(slow.started) == 1
        h.session.state = "busy"
        h.inbox.put(msg(3))
        t0 = time.monotonic()
        h.inbox.close()
        assert time.monotonic() - t0 < 0.1
        await until(lambda: len(slow.started) == 2)
        await h.inbox.drain(0.2)
        assert slow.cancelled == 2
    finally:
        await h.router.close()


async def test_outcome_error_isolated(tmp_path, caplog):
    bad = Recorder(fail=f"sender down {SECRET}")
    h = OutcomeHarness(tmp_path, bad)
    caplog.set_level(logging.DEBUG)
    try:
        h.inbox.put(wish(1))
        await until(lambda: h.submits())
        await h.stop()
        await until(lambda: "sender down" in caplog.text)
        assert SECRET not in caplog.text  # logged redacted
        h.inbox.put(wish(2))
        await until(lambda: len(h.submits()) == 2)
        await h.stop()
        await until(lambda: len(bad.started) == 2)
        assert [d["result"] for d in h.delivered()] == ["delivered", "delivered"]
        await h.inbox.drain(1.0)
    finally:
        await h.router.close()


# ---------------------------------------------------------------- World: real PTY, fake claude


async def start_peer(world: World, on_outcome, scenario: dict | None = None):
    m = world.manager(world.cfg(scenario))
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    router = m.router(sid)
    inbox = PeerInbox(router, max_items=32, max_per_minute=30, on_outcome=on_outcome)
    router.peer_inbox = inbox
    return m, sid, router, inbox


async def test_world_notify_when_idle_delivered_completed(world):
    rec = Recorder()
    m, sid, router, inbox = await start_peer(world, rec)
    plain, asked = msg(1, "plain"), wish(2, "tell me")
    inbox.put(plain)
    inbox.put(asked)
    await world.until(lambda: rec.calls, what="delivered outcome")
    await asyncio.sleep(0.1)
    ((item, status, record),) = rec.calls
    assert item is asked and status == "delivered" and record.state == "completed"
    turns = turns_of(m, sid)
    assert [t.state for t in turns] == ["completed", "completed"]
    assert record.turn_id == turns[1].turn_id


async def test_world_interrupted_handling_turn(world):
    rec = Recorder()
    scenario = {"turns": [{"match": "SLOWPEER", "steps": [{"sleep": 5.0}, {"text": "x"}]}]}
    m, sid, router, inbox = await start_peer(world, rec, scenario)
    item = wish(1, "SLOWPEER")
    inbox.put(item)
    await world.until(
        lambda: router.session.current_turn is not None and router.session.state == "busy",
        what="peer turn running",
    )
    await asyncio.sleep(0.3)
    await router.interrupt()
    await world.until(lambda: rec.calls, what="delivered outcome")
    ((got, status, record),) = rec.calls
    assert got is item and status == "delivered" and record.state == "interrupted"
    assert [t.state for t in turns_of(m, sid)] == ["interrupted"]
