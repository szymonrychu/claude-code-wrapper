"""History paging backwards (before_seq) and the types filter (A3)."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_api_events import SID, make

from claude_code_wrapper.events import EVENT_TYPES, Event, EventLog


def seqs(body: dict) -> list[int]:
    return [e["seq"] for e in body["events"]]


async def test_before_seq_pages_backwards(tmp_path: Path) -> None:
    _, _, c = make(tmp_path, n=10)
    r = await c.get(f"/v1/sessions/{SID}/history", params={"before_seq": 9, "limit": 3})
    body = r.json()
    assert r.status_code == 200
    assert seqs(body) == [6, 7, 8]
    assert body["prev_seq"] == 6 and body["has_more_before"] is True
    r = await c.get(f"/v1/sessions/{SID}/history", params={"before_seq": 6, "limit": 3})
    assert seqs(r.json()) == [3, 4, 5]
    r = await c.get(f"/v1/sessions/{SID}/history", params={"before_seq": 3, "limit": 3})
    body = r.json()
    assert seqs(body) == [1, 2]
    assert body["prev_seq"] is None and body["has_more_before"] is False
    # without before_seq the shape is unchanged
    body = (await c.get(f"/v1/sessions/{SID}/history", params={"limit": 2})).json()
    assert set(body) == {"events", "next_seq", "has_more"}


async def test_types_filter(tmp_path: Path) -> None:
    mgr, log, c = make(tmp_path)
    for i in range(6):
        log.append("assistant_text" if i % 2 else "thinking", {"i": i})
    url = f"/v1/sessions/{SID}/history"
    fwd = (await c.get(url, params={"types": "thinking", "limit": 2})).json()
    assert seqs(fwd) == [1, 3] and fwd["has_more"] is True and fwd["next_seq"] == 3
    fwd = (await c.get(url, params={"types": "thinking", "after_seq": 3})).json()
    assert seqs(fwd) == [5] and fwd["has_more"] is False
    back = (
        await c.get(url, params={"types": "assistant_text", "before_seq": 6, "limit": 2})
    ).json()
    assert seqs(back) == [2, 4] and back["has_more_before"] is False and back["prev_seq"] is None
    both = (await c.get(url, params={"types": "thinking,assistant_text", "limit": 100})).json()
    assert len(both["events"]) == 6


async def test_invalid_combinations_are_400(tmp_path: Path) -> None:
    _, _, c = make(tmp_path, n=3)
    url = f"/v1/sessions/{SID}/history"
    r = await c.get(url, params={"before_seq": 3, "after_seq": 1})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"
    r = await c.get(url, params={"types": "nope"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"


async def test_stopped_session_and_rotation(tmp_path: Path) -> None:
    _, _, c = make(tmp_path, readonly=True, n=5)
    r = await c.get(f"/v1/sessions/{SID}/history", params={"before_seq": 4, "limit": 2})
    assert seqs(r.json()) == [2, 3] and r.json()["prev_seq"] == 2

    # rotation drops the oldest half of the file; the ring still holds recent events
    path = tmp_path / "rot" / "events.jsonl"
    log = EventLog(path, SID, ring_size=4, max_bytes=2000)
    for i in range(60):
        log.append("assistant_text", {"i": i, "pad": "x" * 20})
    on_disk = [e.seq for e in log._read_file()]
    assert on_disk[0] > 1
    events, more = log.page_before(on_disk[0] + 3, 100)
    assert [e.seq for e in events] == [on_disk[0], on_disk[0] + 1, on_disk[0] + 2]
    assert more is False
    events, more = log.page_before(61, 2)
    assert [e.seq for e in events] == [59, 60] and more is True
    log.close()
    ro = EventLog(path, SID, ring_size=4, readonly=True)
    events, more = ro.page_before(on_disk[0] + 3, 2)
    assert [e.seq for e in events] == [on_disk[0] + 1, on_disk[0] + 2] and more is True


@pytest.mark.parametrize("etype", EVENT_TYPES)
async def test_prev_seq_null_and_types_validated_against_event_types(
    tmp_path: Path, etype: str
) -> None:
    _, _, c = make(tmp_path, n=2)
    url = f"/v1/sessions/{SID}/history"
    assert "user_message" in EVENT_TYPES
    r = await c.get(url, params={"types": etype, "before_seq": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["has_more_before"] is False and body["prev_seq"] is None
    r = await c.get(url, params={"types": f"{etype},bogus"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"


async def test_file_scan_runs_off_event_loop(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    import threading

    _, log, c = make(tmp_path, n=5)
    ticked = threading.Event()
    seen: list[bool] = []

    def slow_page_before(self, before_seq, limit=100, types=None):
        seen.append(ticked.wait(3))
        return [], False

    monkeypatch.setattr(EventLog, "page_before", slow_page_before)

    async def tick() -> None:
        await asyncio.sleep(0.05)
        ticked.set()

    task = asyncio.create_task(tick())
    r = await c.get(f"/v1/sessions/{SID}/history", params={"before_seq": 4})
    await task
    assert r.status_code == 200
    assert seen == [True]


def test_page_before_bounded_scan(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog(path, SID, ring_size=2)
    for i in range(2000):
        log.append("assistant_text", {"i": i, "pad": "x" * 50})
    expected = [e.seq for e in log.page_before(1500, 5)[0]]
    assert expected == [1495, 1496, 1497, 1498, 1499]
    parsed = 0
    real = Event.from_json

    def counting(line):
        nonlocal parsed
        parsed += 1
        return real(line)

    monkeypatch.setattr(Event, "from_json", counting)
    events, more = log.page_before(1500, 5)
    assert [e.seq for e in events] == expected and more is True
    assert parsed < 700
    events, more = log.page_before(3, 5)
    assert [e.seq for e in events] == [1, 2] and more is False
