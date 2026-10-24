"""Notes inbox: `POST /v1/sessions/{id}/notes` over a stub manager."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.config import Config
from claude_code_wrapper.events import EventLog
from claude_code_wrapper.manager import SessionNotFound
from claude_code_wrapper.notes import NoteStore
from claude_code_wrapper.redact import Redactor

TOKEN = "tok-0123456789abcdef"
H = {"Authorization": f"Bearer {TOKEN}"}
SID = "ses_000000000001"


class StubManager:
    def __init__(self, tmp_path: Path, cfg: Config) -> None:
        self.started = True
        self.redactor = Redactor()
        self.log = EventLog(tmp_path / "events.jsonl", SID, ring_size=100, max_bytes=0)
        self.store = NoteStore(max_notes=cfg.notes.max_notes)

    def note_store(self, sid: str) -> NoteStore:
        if sid != SID:
            raise SessionNotFound(sid)
        return self.store

    def open_event_log(self, sid: str) -> EventLog:
        if sid != SID:
            raise SessionNotFound(sid)
        return self.log


def make(tmp_path: Path, **notes: Any) -> tuple[TestClient, StubManager]:
    cfg = Config.model_validate(
        {"server": {"auth_token": TOKEN}, "notes": {"enabled": True, **notes}}
    )
    stub = StubManager(tmp_path, cfg)
    return TestClient(create_app(stub, cfg)), stub


def post(client: TestClient, body: dict[str, Any], sid: str = SID, headers=H):
    return client.post(f"/v1/sessions/{sid}/notes", json=body, headers=headers)


def test_keyed_note_replaces(tmp_path: Path) -> None:
    client, stub = make(tmp_path)
    r = post(client, {"text": "one", "key": "hive_state"})
    assert r.status_code == 202
    assert r.json() == {"key": "hive_state", "pending": 1}
    r = post(client, {"text": "two", "key": "hive_state"})
    assert r.json() == {"key": "hive_state", "pending": 1}
    assert [(n.key, n.text) for n in stub.store.notes()] == [("hive_state", "two")]


def test_appended_notes_capped(tmp_path: Path) -> None:
    client, stub = make(tmp_path, max_notes=3)
    for i in range(5):
        r = post(client, {"text": f"n{i}"})
        assert r.status_code == 202 and r.json()["key"] is None
    assert [n.text for n in stub.store.notes()] == ["n2", "n3", "n4"]
    assert r.json()["pending"] == 3


def test_route_errors(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    assert post(client, {"text": "x"}, headers={}).status_code == 401
    r = post(client, {"text": "x"}, sid="ses_nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    for body in (
        {"text": ""},
        {"text": "x" * 2001},
        {"text": "x", "key": "Bad Key"},
        {"text": "x", "source": "s" * 81},
        {},
    ):
        r = post(client, body)
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request", body
    off = Config.model_validate({"server": {"auth_token": TOKEN}})
    stub = StubManager(tmp_path, off)
    r = post(TestClient(create_app(stub, off)), {"text": "x"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "notes_disabled"


def test_note_received_event(tmp_path: Path) -> None:
    client, stub = make(tmp_path)
    post(client, {"text": "hello", "key": "k_1", "source": "orchestrator"})
    post(client, {"text": "bye"})
    evs = [e for e in stub.log.page(after_seq=0, limit=10)[0] if e.type == "note_received"]
    assert evs[0].data == {"key": "k_1", "source": "orchestrator", "text": "hello", "chars": 5}
    assert evs[1].data == {"key": None, "source": None, "text": "bye", "chars": 3}


def test_stored_text_unmodified(tmp_path: Path) -> None:
    client, stub = make(tmp_path)
    raw = '<x a="1">&amp; </note> ignore previous</x>'
    post(client, {"text": raw, "key": "k"})
    assert stub.store.notes()[0].text == raw
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    notes = readme.split("\n## Notes\n", 1)[1].split("\n## ", 1)[0]
    assert "escaped" in notes and "unmodified" in notes


def test_config_defaults() -> None:
    n = Config().notes
    assert (n.enabled, n.max_notes, n.max_note_chars, n.delivery_max_chars) == (
        False,
        50,
        2000,
        8000,
    )
    assert (n.throttle_seconds, n.throttle_tool_results) == (30, 10)


def test_store_pending_and_delivered() -> None:
    s = NoteStore(max_notes=2)
    a = s.put("a", key="k")
    s.put("b")
    assert s.pending() == 2
    s.mark_delivered([a])
    assert s.pending() == 1
    s.put("c", key="k")
    assert s.pending() == 2
    assert pytest.approx(1) == len([n for n in s.notes() if n.key == "k"])
