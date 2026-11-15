"""Pressure policy API: `/v1/sessions/{id}/pressure` over a stub manager."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.config import Config
from claude_code_wrapper.events import EventLog
from claude_code_wrapper.manager import SessionNotFound
from claude_code_wrapper.pressure import PressureState
from claude_code_wrapper.redact import Redactor

TOKEN = "tok-" + "a" * 20
H = {"Authorization": f"Bearer {TOKEN}"}
SID = "ses_000000000001"
URL = f"/v1/sessions/{SID}/pressure"


class StubManager:
    def __init__(self, tmp_path: Path) -> None:
        self.started = True
        self.redactor = Redactor()
        self.log = EventLog(tmp_path / "events.jsonl", SID, ring_size=100, max_bytes=0)
        self.state = PressureState()
        self.context: dict[str, Any] | None = None

    def pressure_state(self, sid: str) -> PressureState:
        if sid != SID:
            raise SessionNotFound(sid)
        return self.state

    def pressure_context(self, sid: str) -> dict[str, Any] | None:
        return self.context

    def open_event_log(self, sid: str) -> EventLog:
        if sid != SID:
            raise SessionNotFound(sid)
        return self.log


def make(tmp_path: Path, enabled: bool = True, **pressure: Any):
    cfg = Config.model_validate(
        {"server": {"auth_token": TOKEN}, "pressure": {"enabled": enabled, **pressure}}
    )
    stub = StubManager(tmp_path)
    return TestClient(create_app(stub, cfg)), stub, cfg


def policy(version: int = 1, **over: Any) -> dict[str, Any]:
    body = {
        "version": version,
        "thresholds": {
            "l1_tokens": 100_000,
            "l2_tokens": 130_000,
            "l3_tokens": 165_000,
            "compact_tokens": 190_000,
            "l1_pct": 50,
            "l2_pct": 65,
            "l3_pct": 82.5,
            "compact_pct": 95,
        },
        "profile": "work",
        "texts": {lv: {"full": "f", "short": "s"} for lv in ("l1", "l2", "l3")},
    }
    body.update(over)
    return body


def test_put_get_delete_roundtrip(tmp_path: Path) -> None:
    client, stub, _ = make(tmp_path)
    stub.context = {"used_tokens": 140_000, "window_tokens": 200_000, "used_percentage": 70}
    r = client.put(URL, json=policy(3), headers=H)
    assert r.status_code == 200
    assert r.json() == {"version": 3, "result": "set", "level": 2}
    r = client.get(URL, headers=H)
    assert r.json() == {
        "version": 3,
        "level": 2,
        "context": stub.context,
        "handoff_state": "none",
        "deliveries": 0,
        "stop_blocks_this_turn": 0,
    }
    assert client.delete(URL, headers=H).status_code == 204
    assert client.get(URL, headers=H).json()["version"] is None


def test_put_refused_when_disabled(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path, enabled=False)
    r = client.put(URL, json=policy(), headers=H)
    assert r.status_code == 409 and r.json()["error"]["code"] == "pressure_disabled"


def test_stale_version_409(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    assert client.put(URL, json=policy(2), headers=H).status_code == 200
    r = client.put(URL, json=policy(1), headers=H)
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_version"


def test_unknown_session_404(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    for call in (client.get, client.delete):
        assert call("/v1/sessions/ses_nope/pressure", headers=H).status_code == 404
    r = client.put("/v1/sessions/ses_nope/pressure", json=policy(), headers=H)
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_get_after_restart_has_no_version(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    client.put(URL, json=policy(4), headers=H)
    client2, _, _ = make(tmp_path)  # new manager, same session id
    assert client2.get(URL, headers=H).json()["version"] is None


def test_policy_set_event_emitted_once(tmp_path: Path) -> None:
    client, stub, _ = make(tmp_path)
    assert client.put(URL, json=policy(2), headers=H).json()["result"] == "set"
    assert client.put(URL, json=policy(2), headers=H).json()["result"] == "unchanged"
    evs = [e for e in stub.log.page(after_seq=0, limit=10)[0] if e.type == "pressure_policy_set"]
    assert [e.data for e in evs] == [{"version": 2, "profile": "work"}]


def test_text_over_max_chars_422(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path, max_text_chars=10)
    body = policy()
    body["texts"]["l2"]["full"] = "x" * 11
    r = client.put(URL, json=body, headers=H)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"
