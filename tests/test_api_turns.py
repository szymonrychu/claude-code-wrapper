"""Turn, interrupt, probe and transcript routes over a stub manager and router."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import World, jsonl
from fastapi.testclient import TestClient
from test_metrics import sample
from test_session_turns import Harness

from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.api.turns import MESSAGE_ID_PATTERN
from claude_code_wrapper.config import Config
from claude_code_wrapper.manager import CreateSessionRequest, InvalidState, SessionNotFound
from claude_code_wrapper.metrics import Metrics
from claude_code_wrapper.redact import Redactor
from claude_code_wrapper.router import Delivery, TurnVetoed
from claude_code_wrapper.session import TurnRejected
from claude_code_wrapper.turns import Probe, TurnRecord, TurnStore, now_iso
from claude_code_wrapper.workdir import FileWriteError

TOKEN = "tok-0123456789abcdef"
SECRET = "sk-ant-oat01-supersecretvalue"
H = {"Authorization": f"Bearer {TOKEN}"}
SID = "ses_000000000001"


class StubSession:
    def __init__(self) -> None:
        self.probes: dict[str, Probe] = {}
        self.reject: str | None = None

    async def probe(self, text=None) -> Probe:
        if self.reject:
            raise TurnRejected(self.reject)
        p = Probe(probe_id="probe-1", text=text or "ping", created_at=now_iso())
        self.probes[p.probe_id] = p
        return p

    def get_probe(self, pid):
        return self.probes.get(pid)


class StubRouter:
    def __init__(self) -> None:
        self.session: Any = StubSession()
        self.calls: list[tuple] = []
        self.raise_on: Exception | None = None
        self.interrupted: str | None = None
        self.lanes: list[str] = []
        self.message_ids: list[str | None] = []

    async def deliver(self, text, *, lane, message_id=None, **kw):
        self.lanes.append(lane)
        self.message_ids.append(message_id)
        return Delivery(lane, record=await self.submit_turn(text, **kw))

    async def submit_turn(
        self, text, *, files=None, callback_url=None, origin="client", metadata=None
    ):
        self.calls.append(("submit", text, files, callback_url, metadata))
        if self.raise_on is not None:
            raise self.raise_on
        return TurnRecord(turn_id="turn-1", session_id=SID, text=text, started_at=now_iso())

    async def interrupt(self):
        return self.interrupted


class StubManager:
    def __init__(self, tmp_path: Path) -> None:
        self.started = True
        self.redactor = Redactor()
        self.router_obj = StubRouter()
        self.router_error: Exception | None = None
        self.turns = TurnStore(tmp_path / "turns.jsonl")
        self.transcript = tmp_path / "t.jsonl"

    def router(self, sid):
        if self.router_error is not None:
            raise self.router_error
        return self.router_obj

    def open_turns(self, sid):
        if sid != SID:
            raise SessionNotFound(sid)
        return self.turns

    def transcript_path(self, sid):
        return str(self.transcript)


@pytest.fixture
def stub(tmp_path: Path) -> StubManager:
    return StubManager(tmp_path)


@pytest.fixture
def client(stub: StubManager) -> TestClient:
    cfg = Config.model_validate(
        {
            "server": {"auth_token": TOKEN},
            "limits": {
                "max_prompt_bytes": 64,
                "max_attachment_bytes": 8,
                "max_attachments_total_bytes": 12,
                "max_attachments_per_turn": 2,
            },
            "prompts": {
                "templates": {
                    "greet": {
                        "description": "d",
                        "variables": {"who": {"required": True}},
                        "text": "hi {{ who }}",
                    }
                }
            },
        }
    )
    return TestClient(create_app(stub, cfg), headers=H)


def post(client: TestClient, body: dict[str, Any], sid: str = SID):
    return client.post(f"/v1/sessions/{sid}/turns", json=body)


def test_submit_turn_202(client: TestClient, stub: StubManager) -> None:
    r = post(client, {"text": "hello", "files": {"a.txt": "x"}, "callbackUrl": "http://h/cb"})
    assert r.status_code == 202
    assert r.json() == {"turn_id": "turn-1", "state": "running", "lane": "regular", "queued": False}
    assert stub.router_obj.calls == [("submit", "hello", {"a.txt": "x"}, "http://h/cb", {})]


def test_template_turn_rendered(client: TestClient, stub: StubManager) -> None:
    r = post(client, {"template": "greet", "variables": {"who": "bob"}})
    assert r.status_code == 202
    assert stub.router_obj.calls[0][1] == "hi bob"


def test_submit_template_errors_are_422(client: TestClient) -> None:
    r = post(client, {"template": "nope"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_template"


@pytest.mark.parametrize("body", [{}, {"text": "a", "template": "greet"}])
def test_text_or_template_400(client: TestClient, body: dict) -> None:
    r = post(client, body)
    assert r.status_code == 400 and r.json()["error"]["code"] == "text_or_template"


def test_prompt_too_large_413(client: TestClient) -> None:
    r = post(client, {"text": "x" * 65})
    assert r.status_code == 413 and r.json()["error"]["code"] == "too_large"


def test_callback_url_must_be_http(client: TestClient) -> None:
    r = post(client, {"text": "a", "callbackUrl": "ftp://x"})
    assert r.status_code == 400


def test_turn_body_uses_callback_url_camel_case(client: TestClient) -> None:
    assert post(client, {"text": "a", "callback_url": "http://x/y"}).status_code == 422


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (TurnRejected("busy"), 409, "busy"),
        (TurnRejected("booting"), 503, "booting"),
        (TurnRejected("dead"), 503, "dead"),
        (TurnVetoed("not now"), 403, "vetoed"),
        (FileWriteError("too_large", "big"), 413, "too_large"),
        (FileWriteError("invalid_path", "bad"), 400, "invalid_path"),
    ],
)
def test_submit_error_mapping(client: TestClient, stub: StubManager, exc, status, code) -> None:
    stub.router_obj.raise_on = exc
    r = post(client, {"text": "a", "lane": "idle"})
    assert r.status_code == status and r.json()["error"]["code"] == code


def test_stopped_session_is_503_dead_and_unknown_404(client: TestClient, stub: StubManager) -> None:
    stub.router_error = InvalidState(SID, "stopped")
    r = post(client, {"text": "a", "lane": "idle"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "dead"
    stub.router_error = SessionNotFound("ses_x")
    assert post(client, {"text": "a"}, "ses_x").status_code == 404


def test_requires_auth(stub: StubManager) -> None:
    cfg = Config.model_validate({"server": {"auth_token": TOKEN}})
    r = TestClient(create_app(stub, cfg)).post(f"/v1/sessions/{SID}/turns", json={"text": "a"})
    assert r.status_code == 401


def test_list_and_get_turn(client: TestClient, stub: StubManager) -> None:
    stub.redactor.add([SECRET])
    rec = TurnRecord(turn_id="turn-1", session_id=SID, text=f"use {SECRET}", started_at=now_iso())
    stub.turns.put(rec)
    listing = client.get(f"/v1/sessions/{SID}/turns").json()["turns"]
    assert [t["turn_id"] for t in listing] == ["turn-1"]
    full = client.get(f"/v1/sessions/{SID}/turns/turn-1")
    assert full.status_code == 200 and SECRET not in full.text and "***" in full.text
    assert client.get(f"/v1/sessions/{SID}/turns/missing").status_code == 404
    assert client.get("/v1/sessions/ses_x/turns").status_code == 404


def test_interrupt_endpoint(client: TestClient, stub: StubManager) -> None:
    r = client.post(f"/v1/sessions/{SID}/interrupt")
    assert r.json() == {"interrupted": False, "turn_id": None}
    stub.router_obj.interrupted = "turn-1"
    r = client.post(f"/v1/sessions/{SID}/interrupt")
    assert r.status_code == 200 and r.json() == {"interrupted": True, "turn_id": "turn-1"}


def test_probe_endpoints(client: TestClient, stub: StubManager) -> None:
    r = client.post(f"/v1/sessions/{SID}/probe", json={"text": "alive?"})
    assert r.status_code == 202 and r.json() == {"probe_id": "probe-1", "state": "pending"}
    assert client.post(f"/v1/sessions/{SID}/probe").status_code == 202
    got = client.get(f"/v1/sessions/{SID}/probe/probe-1").json()
    assert got["state"] == "pending" and got["delivered_at"] is None and got["answered_at"] is None
    assert client.get(f"/v1/sessions/{SID}/probe/nope").status_code == 404
    stub.router_obj.session.reject = "booting"
    assert client.post(f"/v1/sessions/{SID}/probe").status_code == 503


async def test_probe_refuses_slash_text(
    stub: StubManager, tmp_path: Path, fake_claude_bin: Path, scenario_file
) -> None:
    # The real Session.probe behind the route: the guard is the session's, not the stub's.
    harness = Harness(tmp_path, fake_claude_bin, scenario_file)
    session = await harness.start()
    metrics = Metrics()
    cfg = Config.model_validate({"server": {"auth_token": TOKEN}})
    stub.router_obj.session = session
    api = TestClient(create_app(stub, cfg, metrics), headers=H)
    try:
        for text in ("/exit", "\x1b/exit", "/exit now"):
            r = api.post(f"/v1/sessions/{SID}/probe", json={"text": text})
            assert r.status_code == 422, text
            assert r.json()["error"]["code"] == "slash_command", text
        assert harness.writes == []
        assert sample(metrics, "ccw_probe_outcomes_total", outcome="rejected") == 3
        r = api.post(f"/v1/sessions/{SID}/probe", json={"text": "is /exit a command?"})
        assert r.status_code == 202 and r.json()["state"] == "pending"
        assert harness.writes != []
        assert sample(metrics, "ccw_probe_outcomes_total", outcome="sent") == 1
    finally:
        await session.stop(grace=1.0)


def test_transcript_ndjson_redacted(client: TestClient, stub: StubManager) -> None:
    stub.redactor.add([SECRET])
    stub.transcript.write_text(
        json.dumps({"n": 1, "t": SECRET}) + "\n" + json.dumps({"n": 2}) + "\n"
    )
    r = client.get(f"/v1/sessions/{SID}/transcript")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    lines = r.text.splitlines()
    assert [json.loads(x)["n"] for x in lines] == [1, 2]
    assert SECRET not in r.text


def test_transcript_missing_404(client: TestClient) -> None:
    r = client.get(f"/v1/sessions/{SID}/transcript")
    assert r.status_code == 404 and r.json()["error"]["code"] == "no_transcript"


def test_submit_validation_400_413(client: TestClient, stub: StubManager) -> None:
    assert post(client, {}).status_code == 400
    assert post(client, {"text": "a", "template": "greet"}).status_code == 400
    assert post(client, {"text": "a", "callbackUrl": "ftp://x"}).status_code == 400
    assert post(client, {"text": "x" * 65}).status_code == 413
    stub.router_obj.raise_on = FileWriteError("too_large", "big")
    assert post(client, {"text": "a"}).status_code == 413
    stub.router_obj.raise_on = FileWriteError("invalid_path", "bad")
    r = post(client, {"text": "a"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_path"


def test_submit_busy_409_and_booting_503(client: TestClient, stub: StubManager) -> None:
    stub.router_obj.raise_on = TurnRejected("busy")
    assert post(client, {"text": "a"}).status_code == 409
    for reason in ("booting", "dead"):  # idle lane; regular and priority: test_lanes.py
        stub.router_obj.raise_on = TurnRejected(reason)
        r = post(client, {"text": "a", "lane": "idle"})
        assert r.status_code == 503 and r.json()["error"]["code"] == reason


def test_submit_vetoed_403(client: TestClient, stub: StubManager) -> None:
    stub.router_obj.raise_on = TurnVetoed("not now")
    r = post(client, {"text": "a"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "vetoed"


# ------------------------------------------------------------------ slash guard (design D3)


async def _live_app(world: World, **sections: Any) -> tuple[Any, str, httpx.AsyncClient]:
    """A real manager over the fake claude, one ready session, and an API client for it."""
    cfg = world.cfg(server={"auth_token": TOKEN}, **sections)
    m = world.manager(cfg)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    transport = httpx.ASGITransport(app=create_app(m, cfg))
    return m, sid, httpx.AsyncClient(transport=transport, base_url="http://t", headers=H)


async def test_slash_text_turn_is_refused_not_typed(world: World) -> None:
    m, sid, c = await _live_app(world)
    inputs = world.session_dir(sid) / "config" / "fake-inputs.jsonl"
    async with c:
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "/reload-plugins"})
        assert r.status_code == 422
        assert r.json()["error"] == {
            "code": "slash_command",
            "message": "slash commands go to POST /v1/sessions/{id}/commands",
        }
        assert jsonl(inputs) == []  # nothing was typed
        assert m.get(sid)["state"] == "ready"
        assert m.open_turns(sid).list() == []
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "hello"})
        assert r.status_code == 202
    await world.wait_turn(m, sid)
    assert [i["text"] for i in jsonl(inputs)][0].startswith("hello")


async def test_slash_turn_fires_no_on_turn(world: World, tmp_path: Path) -> None:
    on_turn = tmp_path / "on_turn.jsonl"
    hook = {"type": "command", "run": f"cat >> '{on_turn}'; echo >> '{on_turn}'"}
    m, sid, c = await _live_app(world, hooks={"onTurn": [hook]})
    workdir = Path(m.get(sid)["workdir"])
    async with c:
        r = await c.post(
            f"/v1/sessions/{sid}/turns", json={"text": "/compact", "files": {"a.txt": "x"}}
        )
        assert r.status_code == 422 and r.json()["error"]["code"] == "slash_command"
        assert not (workdir / "a.txt").exists()  # no turn file written
        # A prose turn after it fires onTurn once, for itself only.
        r = await c.post(
            f"/v1/sessions/{sid}/turns", json={"text": "hello", "files": {"b.txt": "y"}}
        )
        assert r.status_code == 202
    await world.until(lambda: jsonl(on_turn), what="the prose turn's onTurn")
    await world.wait_turn(m, sid)
    assert [e["data"]["text"] for e in jsonl(on_turn)] == ["hello"]
    assert (workdir / "b.txt").read_text() == "y"
    assert not (workdir / "a.txt").exists()


def att(name: str, raw: bytes = b"PNGDATA", mime: str = "image/png") -> dict[str, str]:
    return {"name": name, "mime": mime, "data_b64": base64.b64encode(raw).decode()}


def test_attachments_merge_into_files(client: TestClient, stub: StubManager) -> None:
    r = post(client, {"text": "hi", "message_id": "m-1", "attachments": [att("a.png")]})
    assert r.status_code == 202, r.text
    assert r.json()["attachments"] == [".attachments/m-1/a.png"]
    assert r.json()["message_id"] == "m-1"
    assert stub.router_obj.calls[0][2] == {
        ".attachments/.gitignore": b"*\n",
        ".attachments/m-1/a.png": b"PNGDATA",
    }
    assert stub.router_obj.message_ids == ["m-1"]


def test_attachments_generate_message_id(client: TestClient, stub: StubManager) -> None:
    r = post(client, {"text": "hi", "attachments": [att("a.png")]})
    assert r.status_code == 202, r.text
    mid = r.json()["message_id"]
    assert re.match(MESSAGE_ID_PATTERN, mid)
    assert r.json()["attachments"] == [f".attachments/{mid}/a.png"]
    assert stub.router_obj.message_ids == [mid]


def test_attachments_and_text_files_together(client: TestClient, stub: StubManager) -> None:
    r = post(client, {"text": "hi", "files": {"n.txt": "x"}, "attachments": [att("a.png")]})
    assert r.status_code == 202, r.text
    files = stub.router_obj.calls[0][2]
    assert files["n.txt"] == "x"
    assert any(k.endswith("/a.png") for k in files)


def test_no_attachments_leaves_response_unchanged(client: TestClient) -> None:
    r = post(client, {"text": "hi", "attachments": []})
    assert r.status_code == 202
    assert "attachments" not in r.json()


@pytest.mark.parametrize("data_b64", ["@@@", "QUJD\nRA=="])
def test_attachment_bad_base64_400(client: TestClient, stub: StubManager, data_b64: str) -> None:
    a = {"name": "a.png", "mime": "image/png", "data_b64": data_b64}
    r = post(client, {"text": "hi", "attachments": [a]})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_attachment"
    assert stub.router_obj.calls == []


@pytest.mark.parametrize(
    "attachments",
    [
        [att("../x")],
        [att(".gitignore")],
        [att("a.png", b"1"), att("a.png", b"2")],
        [att("a.png", mime="PNG")],
    ],
)
def test_attachment_bad_name_400(client: TestClient, stub: StubManager, attachments) -> None:
    r = post(client, {"text": "hi", "attachments": attachments})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_attachment"
    assert stub.router_obj.calls == []


@pytest.mark.parametrize("message_id", [".", ".."])
def test_attachment_dot_message_id_refused(
    client: TestClient, stub: StubManager, message_id
) -> None:
    # the request pattern already refuses a leading dot (422); the handler check is a second line
    r = post(client, {"text": "hi", "message_id": message_id, "attachments": [att("a.png")]})
    assert r.status_code == 422
    assert stub.router_obj.calls == []


def test_attachment_message_id_with_colon_400(client: TestClient, stub: StubManager) -> None:
    r = post(client, {"text": "hi", "message_id": "a:b", "attachments": [att("a.png")]})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_attachment"


@pytest.mark.parametrize(
    "attachments",
    [
        [att("a.png", b"123456789")],
        [att("a.png", b"1234567"), att("b.png", b"1234567")],
        [att("a.png", b"1"), att("b.png", b"1"), att("c.png", b"1")],
    ],
)
def test_attachment_caps_413(client: TestClient, stub: StubManager, attachments) -> None:
    r = post(client, {"text": "hi", "attachments": attachments})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "too_large"
    assert stub.router_obj.calls == []


def test_attachment_oversize_not_decoded(
    client: TestClient, stub: StubManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a, **k):
        raise AssertionError("decoded an oversize attachment")

    monkeypatch.setattr(base64, "b64decode", boom)
    a = {"name": "a.png", "mime": "image/png", "data_b64": "A" * 10000}
    r = post(client, {"text": "hi", "attachments": [a]})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "too_large"


def test_attachment_zero_bytes_400(client: TestClient) -> None:
    r = post(client, {"text": "hi", "attachments": [att("a.png", b"")]})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_attachment"
