"""`GET /v1/sessions/{id}/inflight` over a stub manager."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.config import Config
from claude_code_wrapper.manager import SessionNotFound
from claude_code_wrapper.redact import Redactor

TOKEN = "tok-" + "a" * 20
H = {"Authorization": f"Bearer {TOKEN}"}
SID = "ses_000000000001"
URL = f"/v1/sessions/{SID}/inflight"


class StubManager:
    def __init__(self, tmp_path: Path, scratch: Path) -> None:
        self.started = True
        self.redactor = Redactor()
        self.router = SimpleNamespace(stop_inventory=None, outstanding_subagent_ids=[])
        self.workdir = tmp_path / "work"
        self.workdir.mkdir()
        self.scratch = scratch

    def inflight_source(self, sid: str) -> Any:
        if sid != SID:
            raise SessionNotFound(sid)
        since = datetime.now(UTC) - timedelta(hours=1)
        return SimpleNamespace(router=self.router, workdir=self.workdir, since=since)


def make(tmp_path: Path):
    pod = tmp_path / "pod"
    cfg = Config.model_validate(
        {"server": {"auth_token": TOKEN}, "pressure": {"scratch_roots": [str(pod / "claude-*")]}}
    )
    stub = StubManager(tmp_path, pod)
    return TestClient(create_app(stub, cfg)), stub


def test_inflight_route_shape(tmp_path: Path) -> None:
    client, stub = make(tmp_path)
    stub.router.outstanding_subagent_ids = ["ag1"]
    stub.router.stop_inventory = {
        "background_tasks": [{"id": "b1", "status": "running"}, {"id": "b2", "status": "done"}],
        "session_crons": [{"id": "c1"}],
        "at": "2026-01-01T00:00:00.000Z",
    }
    r = client.get(URL, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "subagents",
        "subagent_ids",
        "background_tasks",
        "crons",
        "worktrees",
        "scratch",
        "unknown",
        "stop_seen_at",
        "scanned_at",
    }
    assert body["subagents"] == 1 and body["subagent_ids"] == ["ag1"]
    assert [t["id"] for t in body["background_tasks"]] == ["b1"]
    assert body["crons"] == [{"id": "c1"}]
    assert body["stop_seen_at"] == "2026-01-01T00:00:00.000Z" and body["scanned_at"]
    assert body["unknown"] is False


def test_inflight_route_has_scratch(tmp_path: Path) -> None:
    client, stub = make(tmp_path)
    f = tmp_path / "pod" / "claude-1" / "pad.md"
    f.parent.mkdir(parents=True)
    f.write_text("x")
    body = client.get(URL, headers=H).json()
    assert [s["path"] for s in body["scratch"]] == [str(f)]
    assert set(body["scratch"][0]) == {"path", "size", "mtime"}


def test_inflight_unknown_session_404(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    r = client.get("/v1/sessions/ses_nope/inflight", headers=H)
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_inflight_before_any_stop_has_no_tasks(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    body = client.get(URL, headers=H).json()
    assert body["background_tasks"] == [] and body["crons"] == []
    assert body["stop_seen_at"] is None
