"""Tests for the turn store (src/claude_code_wrapper/turns.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_code_wrapper import turns
from claude_code_wrapper.turns import (
    SUMMARY_FIELDS,
    StoreReadOnly,
    TurnRecord,
    TurnStore,
    new_probe_id,
    new_turn_id,
)


def _record(turn_id: str, state: str = "running", **kw) -> TurnRecord:
    base = {
        "turn_id": turn_id,
        "session_id": "ses_0123456789ab",
        "origin": "client",
        "state": state,
        "text": f"prompt for {turn_id}",
        "started_at": "2026-10-20T10:00:00.000+00:00",
    }
    base.update(kw)
    return TurnRecord(**base)


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_turn_ids_monotonic_unique(monkeypatch: pytest.MonkeyPatch) -> None:
    # A clock that repeats, then jumps backwards, then moves on.
    ticks = iter([1000, 1000, 1000, 400, 400, 2000, 1999, 5000])
    monkeypatch.setattr(turns, "_ns_clock", lambda: next(ticks))
    monkeypatch.setattr(turns, "_last_ns", 0)  # ids from earlier tests are not this clock's
    ids = [new_turn_id() for _ in range(5)] + [new_probe_id()] + [new_turn_id() for _ in range(2)]
    assert all(i.startswith("turn-") for i in ids[:5] + ids[6:])
    assert ids[5].startswith("probe-")
    values = [int(i.split("-", 1)[1], 36) for i in ids]
    assert all(b > a for a, b in zip(values, values[1:], strict=False))
    assert len(set(ids)) == len(ids)
    assert values[0] >= 1000
    assert values[-1] == 5000  # a clock ahead of the counter is taken as is
    # The suffix is base36 of nanoseconds: digits and lowercase letters only.
    monkeypatch.setattr(turns, "_ns_clock", lambda: 36**4 * 10**6)
    tid = new_turn_id()
    assert tid == "turn-" + turns.base36(36**4 * 10**6)
    assert set(tid[5:]) <= set("0123456789abcdefghijklmnopqrstuvwxyz")
    assert turns.base36(0) == "0" and turns.base36(35) == "z" and turns.base36(36) == "10"


def test_store_persists_and_reloads_last_state(tmp_path: Path) -> None:
    path = tmp_path / "ses" / "turns.jsonl"  # the parent dir is created on first put
    store = TurnStore(path)
    assert store.list() == [] and store.current is None
    first = _record("turn-a")
    store.put(first)
    assert store.current is not None and store.current.turn_id == "turn-a"
    first.state = "completed"
    first.final_text = "done"
    first.completed_at = "2026-10-20T10:00:05.000+00:00"
    # The store holds what was put, not the live object: a later unsaved change is not seen.
    assert store.get("turn-a").state == "running"
    store.put(first)
    assert store.current is None
    second = _record("turn-b", started_at="2026-10-20T10:01:00.000+00:00")
    store.put(second)
    assert [line["turn_id"] for line in _lines(path)] == ["turn-a", "turn-a", "turn-b"]
    assert _lines(path)[0]["state"] == "running"  # every change is a full appended record

    # A corrupt line and a torn (unterminated) final line are skipped on open.
    with path.open("a") as f:
        f.write("{not json\n")
        f.write(json.dumps({"turn_id": "turn-x", "state": "bogus"}) + "\n")
        f.write('{"turn_id": "turn-c", "sess')
    reopened = TurnStore(path)
    got = reopened.get("turn-a")
    assert got is not None and got.state == "completed" and got.final_text == "done"
    assert reopened.get("turn-x") is None and reopened.get("turn-c") is None
    assert reopened.current is not None and reopened.current.turn_id == "turn-b"
    assert reopened.get("missing") is None
    # Appending after a torn line starts on a fresh line, so the new record survives a reload.
    second.state = "interrupted"
    reopened.put(second)
    again = TurnStore(path)
    assert again.get("turn-b").state == "interrupted"
    assert [r.turn_id for r in again.list()] == ["turn-a", "turn-b"]


def test_list_summaries(tmp_path: Path) -> None:
    store = TurnStore(tmp_path / "turns.jsonl")
    a = _record("turn-a", started_at="2026-10-20T10:00:00.000+00:00")
    b = _record("turn-b", origin="initial", started_at="2026-10-20T10:01:00.000+00:00")
    c = _record("turn-c", origin="agent", started_at="2026-10-20T10:02:00.000+00:00")
    for r in (a, b, c):
        store.put(r)
    # A later update to the first turn does not move it: the list stays in start order.
    a.state = "completed"
    a.stop_reason = "end_turn"
    a.completed_at = "2026-10-20T10:00:30.000+00:00"
    store.put(a)
    listed = store.list()
    assert [r.turn_id for r in listed] == ["turn-a", "turn-b", "turn-c"]
    assert [r.turn_id for r in TurnStore(tmp_path / "turns.jsonl").list()] == [
        "turn-a",
        "turn-b",
        "turn-c",
    ]
    summary = listed[0].summary()
    assert SUMMARY_FIELDS == (
        "turn_id",
        "origin",
        "state",
        "started_at",
        "completed_at",
        "stop_reason",
    )
    assert summary == {
        "turn_id": "turn-a",
        "origin": "client",
        "state": "completed",
        "started_at": "2026-10-20T10:00:00.000+00:00",
        "completed_at": "2026-10-20T10:00:30.000+00:00",
        "stop_reason": "end_turn",
    }
    assert [r.summary()["origin"] for r in listed] == ["client", "initial", "agent"]


def test_readonly_store(tmp_path: Path) -> None:
    path = tmp_path / "turns.jsonl"
    writer = TurnStore(path)
    writer.put(_record("turn-a", state="completed"))
    writer.put(_record("turn-b"))
    before = path.read_text()
    ro = TurnStore(path, readonly=True)
    assert ro.readonly and not writer.readonly
    assert ro.get("turn-a").state == "completed"
    assert [r.turn_id for r in ro.list()] == ["turn-a", "turn-b"]
    assert ro.current.turn_id == "turn-b"
    with pytest.raises(StoreReadOnly):
        ro.put(_record("turn-c"))
    assert path.read_text() == before
    assert ro.get("turn-c") is None
    # A read-only store of a session that never ran a turn is empty, and creates nothing.
    empty = TurnStore(tmp_path / "none" / "turns.jsonl", readonly=True)
    assert empty.list() == [] and empty.current is None
    assert not (tmp_path / "none").exists()


def test_turn_record_serializes_callback_url_camel_case(tmp_path: Path) -> None:
    by_name = _record("turn-a", callback_url="https://hooks.example/cb")
    by_alias = TurnRecord.model_validate(
        {**_record("turn-b").model_dump(), "callbackUrl": "https://hooks.example/cb2"}
    )
    assert by_name.callback_url == "https://hooks.example/cb"
    assert by_alias.callback_url == "https://hooks.example/cb2"
    dumped = by_name.model_dump()
    assert dumped["callbackUrl"] == "https://hooks.example/cb"
    assert "callback_url" not in dumped
    as_json = json.loads(by_name.model_dump_json())
    assert as_json["callbackUrl"] == "https://hooks.example/cb" and "callback_url" not in as_json
    assert set(dumped) == {
        "turn_id",
        "session_id",
        "origin",
        "state",
        "text",
        "final_text",
        "stop_reason",
        "usage",
        "model_tokens",
        "error",
        "callbackUrl",
        "started_at",
        "completed_at",
        "last_activity_at",
        "stall_suspected_since",
        "tool_calls",
        "annotations",
        "metadata",
    }
    path = tmp_path / "turns.jsonl"
    store = TurnStore(path)
    store.put(by_name)
    line = _lines(path)[0]
    assert line["callbackUrl"] == "https://hooks.example/cb" and "callback_url" not in line
    assert TurnStore(path).get("turn-a").callback_url == "https://hooks.example/cb"
    # Defaults: no callback, zero tool calls, nothing finished yet.
    plain = _record("turn-c")
    assert plain.model_dump()["callbackUrl"] is None
    assert plain.tool_calls == 0 and plain.final_text is None and plain.completed_at is None
    with pytest.raises(ValueError):
        _record("turn-d", origin="robot")
    with pytest.raises(ValueError):
        _record("turn-e", state="paused")


def test_refresh_updates_view_without_append(tmp_path: Path) -> None:
    path = tmp_path / "turns.jsonl"
    store = TurnStore(path)
    record = _record("turn-a")
    store.put(record)
    record.last_activity_at = "2026-10-20T10:00:05.000+00:00"
    store.refresh(record)
    assert store.get("turn-a").last_activity_at == "2026-10-20T10:00:05.000+00:00"
    assert len(_lines(path)) == 1  # the view moved; the file did not grow
    record.last_activity_at = "2026-10-20T10:00:06.000+00:00"
    assert store.get("turn-a").last_activity_at == "2026-10-20T10:00:05.000+00:00"  # a copy
    with pytest.raises(StoreReadOnly):
        TurnStore(path, readonly=True).refresh(record)
