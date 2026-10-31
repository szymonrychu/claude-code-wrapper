import asyncio
import json

import pytest

from claude_code_wrapper.events import EVENT_TYPES, Event, EventLog


def test_event_types_exact():
    assert EVENT_TYPES == [
        "session_state",
        "turn_started",
        "user_message",
        "turn_rejected",
        "turn_completed",
        "turn_failed",
        "turn_continued",
        "turn_deferred",
        "turn_resumed",
        "prompt_queued",
        "prompt_delivered",
        "prompt_dropped",
        "assistant_text",
        "thinking",
        "usage",
        "tool_call",
        "tool_result",
        "tool_denied",
        "question",
        "subagent_started",
        "subagent_stopped",
        "subagent_linked",
        "interrupted",
        "stall_suspected",
        "probe",
        "hook_result",
        "command_queued",
        "command_started",
        "command_completed",
        "command_failed",
        "compacting",
        "compacted",
        "context",
        "error",
        "destroy_output",
        "peer_message_received",
        "peer_message_sent",
        "peer_message_delivered",
        "peer_member_joined",
        "peer_member_left",
        "note_received",
        "note_delivered",
        "session_closed",
    ]


async def test_append_and_unknown_type(tmp_path):
    log = EventLog(tmp_path / "e.jsonl", "s1")
    e = log.append("probe", {"a": 1})
    assert (e.seq, e.session_id, e.type, e.data) == (1, "s1", "probe", {"a": 1})
    assert e.ts.endswith("Z") and "." in e.ts
    assert json.loads(e.to_json())["seq"] == 1
    with pytest.raises(ValueError):
        log.append("nope", {})
    assert len((tmp_path / "e.jsonl").read_text().splitlines()) == 1


async def test_seq_continues_and_corrupt_tail(tmp_path):
    p = tmp_path / "e.jsonl"
    log = EventLog(p, "s1")
    log.append("probe", {})
    log.append("probe", {})
    log.close()
    with p.open("a") as f:
        f.write('{"seq": 9, "tr')
    log2 = EventLog(p, "s1")
    assert log2.append("probe", {}).seq == 3


async def test_page_ring_and_file(tmp_path):
    p = tmp_path / "e.jsonl"
    log = EventLog(p, "s1", ring_size=3)
    for i in range(10):
        log.append("probe", {"i": i})
    evs, more = log.page(after_seq=8)
    assert [e.seq for e in evs] == [9, 10] and not more
    evs, more = log.page(after_seq=0, limit=4)
    assert [e.seq for e in evs] == [1, 2, 3, 4] and more
    assert [e.seq for e in log.last(2)] == [9, 10]
    evs, _ = log.page(0, limit=5000)
    assert len(evs) == 10


async def test_rotation_keeps_newest_half(tmp_path):
    p = tmp_path / "e.jsonl"
    log = EventLog(p, "s1", max_bytes=2000)
    for i in range(40):
        log.append("probe", {"i": i})
    lines = p.read_text().splitlines()
    assert len(lines) < 40
    assert json.loads(lines[-1])["seq"] == 40
    assert log.append("probe", {}).seq == 41


async def test_subscribe_fanout_and_close(tmp_path):
    log = EventLog(tmp_path / "e.jsonl", "s1")
    sub = log.subscribe()
    log.append("probe", {})
    got = await asyncio.wait_for(sub.__anext__(), 1)
    assert got.seq == 1
    log.close()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(sub.__anext__(), 1)


async def test_lagging_subscriber_dropped(tmp_path):
    p = tmp_path / "e.jsonl"
    log = EventLog(p, "s1")
    sub = log.subscribe()
    for _ in range(1001):
        log.append("probe", {})
    seen = [e async for e in sub]
    assert seen[-1].type == "error" and seen[-1].data == {"message": "subscriber lagged"}
    assert len(seen) == 1001
    assert len(p.read_text().splitlines()) == 1001


async def test_unsubscribe_ends(tmp_path):
    log = EventLog(tmp_path / "e.jsonl", "s1")
    sub = log.subscribe()
    log.unsubscribe(sub)
    assert [e async for e in sub] == []


async def test_append_threadsafe(tmp_path):
    log = EventLog(tmp_path / "e.jsonl", "s1")
    sub = log.subscribe()
    await asyncio.get_running_loop().run_in_executor(None, log.append_threadsafe, "probe", {"x": 1})
    got = await asyncio.wait_for(sub.__anext__(), 1)
    assert got.data == {"x": 1}


async def test_write_failure_logged_once(tmp_path, caplog):
    log = EventLog(tmp_path / "e.jsonl", "s1")
    log._fh.close()
    caplog.set_level("ERROR")
    log.append("probe", {})
    log.append("probe", {})
    assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 1
    assert log.last(2)[-1].seq == 2


async def test_readonly(tmp_path):
    p = tmp_path / "e.jsonl"
    log = EventLog(p, "s1")
    log.append("probe", {})
    log.close()
    ro = EventLog(p, "s1", readonly=True)
    assert [e.seq for e in ro.page()[0]] == [1]
    assert [e.seq for e in ro.last(5)] == [1]
    with pytest.raises(RuntimeError):
        ro.append("probe", {})
    assert [e async for e in ro.subscribe()] == []
    assert isinstance(ro.last(1)[0], Event)


async def test_append_assigns_increasing_seq(tmp_path):
    p = tmp_path / "e.jsonl"
    log = EventLog(p, "s1")
    seqs = [log.append("probe", {"i": i}).seq for i in range(5)]
    assert seqs == [1, 2, 3, 4, 5]
    assert [json.loads(line)["seq"] for line in p.read_text().splitlines()] == seqs


async def test_seq_continues_after_reopen(tmp_path):
    p = tmp_path / "e.jsonl"
    log = EventLog(p, "s1")
    log.append("probe", {})
    log.append("probe", {})
    log.close()
    assert EventLog(p, "s1").append("probe", {}).seq == 3


async def test_page_after_seq_and_limit(tmp_path):
    log = EventLog(tmp_path / "e.jsonl", "s1")
    for _ in range(10):
        log.append("probe", {})
    evs, more = log.page(after_seq=2, limit=3)
    assert [e.seq for e in evs] == [3, 4, 5] and more
    evs, more = log.page(after_seq=7, limit=3)
    assert [e.seq for e in evs] == [8, 9, 10] and not more


async def test_page_falls_back_to_file_beyond_ring(tmp_path):
    log = EventLog(tmp_path / "e.jsonl", "s1", ring_size=3)
    for _ in range(10):
        log.append("probe", {})
    evs, more = log.page(after_seq=0, limit=100)
    assert [e.seq for e in evs] == list(range(1, 11)) and not more


async def test_subscriber_receives_live_events(tmp_path):
    log = EventLog(tmp_path / "e.jsonl", "s1")
    sub = log.subscribe()
    for i in range(3):
        log.append("probe", {"i": i})
    got = [await asyncio.wait_for(sub.__anext__(), 1) for _ in range(3)]
    assert [e.seq for e in got] == [1, 2, 3]


async def test_write_failure_does_not_raise(tmp_path, caplog):
    log = EventLog(tmp_path / "e.jsonl", "s1")
    log._fh.close()
    caplog.set_level("ERROR")
    assert log.append("probe", {}).seq == 1
    log.append("probe", {})
    assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 1


async def test_unknown_event_type_rejected(tmp_path):
    log = EventLog(tmp_path / "e.jsonl", "s1")
    with pytest.raises(ValueError):
        log.append("bogus", {})


async def test_close_ends_subscriptions(tmp_path):
    log = EventLog(tmp_path / "e.jsonl", "s1")
    sub = log.subscribe()
    log.close()
    assert [e async for e in sub] == []


async def test_readonly_log_pages_and_rejects_append(tmp_path):
    p = tmp_path / "e.jsonl"
    log = EventLog(p, "s1")
    log.append("probe", {})
    log.close()
    ro = EventLog(p, "s1", readonly=True)
    assert [e.seq for e in ro.page()[0]] == [1]
    with pytest.raises(RuntimeError):
        ro.append("probe", {})


async def test_partial_trailing_line_does_not_swallow_next_event(tmp_path):
    p = tmp_path / "e.jsonl"
    log = EventLog(p, "s1")
    log.append("probe", {})
    log.append("probe", {})
    log.close()
    with p.open("a") as f:
        f.write('{"seq": 9, "tr')
    log2 = EventLog(p, "s1")
    assert log2.append("probe", {}).seq == 3
    log2.close()
    ro = EventLog(p, "s1", readonly=True)
    assert [e.seq for e in ro.page()[0]] == [1, 2, 3]


async def test_rotation_failure_keeps_log_writable(tmp_path, monkeypatch):
    import os

    p = tmp_path / "e.jsonl"
    log = EventLog(p, "s1", max_bytes=500)
    real = os.replace
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("boom")
        return real(a, b)

    monkeypatch.setattr(os, "replace", flaky)
    for _ in range(30):
        log.append("probe", {})
    log.close()
    assert calls["n"] >= 2
    last = json.loads(p.read_text().splitlines()[-1])
    assert last["seq"] == 30


async def test_corrupt_tail_mid_multibyte(tmp_path):
    p = tmp_path / "e.jsonl"
    log = EventLog(p, "s1")
    log.append("probe", {"t": "zażółć"})
    log.close()
    with p.open("ab") as f:
        f.write('{"seq": 2, "data": "ż'.encode()[:-1])
    log2 = EventLog(p, "s1")
    assert log2.append("probe", {}).seq == 2
    assert [e.seq for e in log2.page()[0]][:1] == [1]


async def test_rotation_survives_corrupt_tail(tmp_path):
    p = tmp_path / "e.jsonl"
    log = EventLog(p, "s1")
    log.append("probe", {})
    log.close()
    with p.open("ab") as f:
        f.write('{"seq": 2, "data": "ż'.encode()[:-1])
    log2 = EventLog(p, "s1", max_bytes=600)
    for _ in range(30):
        log2.append("probe", {"pad": "x" * 40})
    assert p.stat().st_size <= 600 + 200
    assert log2.last(1)[0].seq == 31
