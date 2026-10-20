"""History and SSE routes over real EventLogs."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.config import Config
from claude_code_wrapper.events import EventLog
from claude_code_wrapper.manager import SessionNotFound
from claude_code_wrapper.redact import Redactor

TOKEN = "tok-0123456789abcdef"
SECRET = "sk-ant-oat01-supersecretvalue"
H = {"Authorization": f"Bearer {TOKEN}"}
SID = "ses_000000000001"


class StubManager:
    def __init__(self, log: EventLog) -> None:
        self.started = True
        self.redactor = Redactor()
        self.log = log

    def open_event_log(self, sid):
        if sid != SID:
            raise SessionNotFound(sid)
        return self.log


def make(tmp_path: Path, *, readonly: bool = False, n: int = 0, **limits: Any):
    path = tmp_path / "events.jsonl"
    if readonly:
        seed = EventLog(path, SID)
        for i in range(n):
            seed.append("assistant_text", {"i": i})
        seed.append("session_closed", {})
        seed.close()
        log = EventLog(path, SID, readonly=True)
    else:
        log = EventLog(path, SID)
        for i in range(n):
            log.append("assistant_text", {"i": i})
    cfg = Config.model_validate(
        {"server": {"auth_token": TOKEN}, "limits": {"sse_heartbeat_seconds": 0.1, **limits}}
    )
    mgr = StubManager(log)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(mgr, cfg)), base_url="http://t", headers=H
    )
    return mgr, log, client


def frames(text: str) -> list[dict[str, Any]]:
    out = []
    for block in text.split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        f = dict(line.split(": ", 1) for line in block.splitlines())
        f["data"] = json.loads(f["data"])
        out.append(f)
    return out


async def close_later(log: EventLog, delay: float, extra: int = 0) -> None:
    await asyncio.sleep(delay)
    for i in range(extra):
        log.append("assistant_text", {"live": i})
    log.append("session_closed", {})
    log.close()


async def test_history_paging(tmp_path: Path) -> None:
    mgr, _, client = make(tmp_path, n=5)
    r = await client.get(f"/v1/sessions/{SID}/history", params={"limit": 2})
    body = r.json()
    assert [e["seq"] for e in body["events"]] == [1, 2]
    assert body["next_seq"] == 2 and body["has_more"] is True
    r = await client.get(f"/v1/sessions/{SID}/history", params={"after_seq": 4, "limit": 5000})
    body = r.json()
    assert [e["seq"] for e in body["events"]] == [5] and body["has_more"] is False
    assert body["next_seq"] == 5
    r = await client.get(f"/v1/sessions/{SID}/history", params={"after_seq": 5})
    assert r.json() == {"events": [], "next_seq": 5, "has_more": False}
    assert (await client.get("/v1/sessions/ses_x/history")).status_code == 404


async def test_history_redacts(tmp_path: Path) -> None:
    mgr, log, client = make(tmp_path)
    mgr.redactor.add([SECRET])
    log.append("assistant_text", {"v": SECRET})
    r = await client.get(f"/v1/sessions/{SID}/history")
    assert SECRET not in r.text


async def test_events_requires_auth(tmp_path: Path) -> None:
    mgr, log, client = make(tmp_path, readonly=True, n=1)
    r = await client.get(f"/v1/sessions/{SID}/events", headers={"Authorization": "Bearer no"})
    assert r.status_code == 401


async def test_stopped_session_replays_then_closes(tmp_path: Path) -> None:
    _, _, client = make(tmp_path, readonly=True, n=3, sse_replay=2)
    r = await client.get(f"/v1/sessions/{SID}/events")
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache" and r.headers["x-accel-buffering"] == "no"
    fs = frames(r.text)
    assert [f["id"] for f in fs] == ["3", "4"]
    assert [f["event"] for f in fs] == ["assistant_text", "session_closed"]
    assert fs[0]["data"]["type"] == "assistant_text" and fs[0]["data"]["seq"] == 3


async def test_stopped_without_closed_event_gets_synthetic(tmp_path: Path) -> None:
    _, log, client = make(tmp_path, readonly=True, n=1)
    log.close()
    # readonly log reread without the closing event
    path = tmp_path / "events.jsonl"
    lines = path.read_text().splitlines()[:-1]
    path.write_text("\n".join(lines) + "\n")
    log2 = EventLog(path, SID, readonly=True)
    mgr = StubManager(log2)
    cfg = Config.model_validate({"server": {"auth_token": TOKEN}})
    c2 = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(mgr, cfg)), base_url="http://t", headers=H
    )
    fs = frames((await c2.get(f"/v1/sessions/{SID}/events")).text)
    assert [f["event"] for f in fs] == ["assistant_text", "session_closed"]


async def test_after_seq_and_last_event_id(tmp_path: Path) -> None:
    _, _, client = make(tmp_path, readonly=True, n=5)
    fs = frames((await client.get(f"/v1/sessions/{SID}/events", params={"after_seq": 4})).text)
    assert [f["id"] for f in fs] == ["5", "6"]
    r = await client.get(f"/v1/sessions/{SID}/events", headers={"Last-Event-ID": "5"})
    assert [f["id"] for f in frames(r.text)] == ["6"]
    r = await client.get(
        f"/v1/sessions/{SID}/events", params={"after_seq": 0}, headers={"Last-Event-ID": "5"}
    )
    assert [f["id"] for f in frames(r.text)] == ["6"]
    r = await client.get(f"/v1/sessions/{SID}/events", headers={"Last-Event-ID": "zz"})
    assert r.status_code == 400


async def test_live_events_deduped_and_heartbeat(tmp_path: Path) -> None:
    _, log, client = make(tmp_path, n=3)
    task = asyncio.create_task(close_later(log, 0.35, extra=2))
    r = await client.get(f"/v1/sessions/{SID}/events")
    await task
    assert ": ping" in r.text
    fs = frames(r.text)
    assert [f["id"] for f in fs] == ["1", "2", "3", "4", "5", "6"]
    assert fs[-1]["event"] == "session_closed"
    assert r.text.index("id: 3") < r.text.index(": ping") < r.text.index("id: 4")


async def test_events_redacted(tmp_path: Path) -> None:
    mgr, log, client = make(tmp_path)
    mgr.redactor.add([SECRET])
    log.append("assistant_text", {"v": SECRET})
    task = asyncio.create_task(close_later(log, 0.05))
    r = await client.get(f"/v1/sessions/{SID}/events")
    await task
    assert SECRET not in r.text and "id: 1" in r.text


async def test_unknown_session_404(tmp_path: Path) -> None:
    _, _, client = make(tmp_path)
    assert (await client.get("/v1/sessions/ses_x/events")).status_code == 404
