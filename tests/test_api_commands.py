"""Public commands routes over a stub manager and a real CommandQueue."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.commands import CommandQueue, CommandRejected, validate_params
from claude_code_wrapper.config import CommandsConfig, Config
from claude_code_wrapper.manager import InvalidState, SessionNotFound
from claude_code_wrapper.redact import Redactor

TOKEN = "tok-0123456789abcdef"
H = {"Authorization": f"Bearer {TOKEN}"}
SID = "ses_000000000001"
BASE = f"/v1/sessions/{SID}/commands"


class StubRouter:
    def __init__(self) -> None:
        self.commands = CommandQueue(CommandsConfig(client=["compact", "reload-plugins"]))
        self.state = "ready"

    def request_command(self, name, params=None, *, origin):
        if self.state != "ready":
            raise CommandRejected("invalid_state", f"session is {self.state}")
        validate_params(name, params, allow_force=False)
        record, _ = self.commands.request(name, params, origin=origin, turn_id=None)
        return record


class StubManager:
    def __init__(self) -> None:
        self.started = True
        self.redactor = Redactor()
        self.router_obj = StubRouter()
        self.stopped = False

    def router(self, sid):
        if sid != SID:
            raise SessionNotFound(sid)
        if self.stopped:
            raise InvalidState(sid, "stopped")
        return self.router_obj


@pytest.fixture
def stub() -> StubManager:
    return StubManager()


@pytest.fixture
def client(stub: StubManager) -> TestClient:
    cfg = Config.model_validate({"server": {"auth_token": TOKEN}})
    return TestClient(create_app(stub, cfg), headers=H)


def test_post_command_202_record(client: TestClient) -> None:
    r = client.post(BASE, json={"name": "compact", "args": {"instructions": "keep api"}})
    assert r.status_code == 202
    body = r.json()
    assert body["origin"] == "client" and body["state"] == "queued"
    assert body["name"] == "compact" and body["args"] == {"instructions": "keep api"}
    assert body["id"].startswith("cmd_")


def test_args_default_and_extra_forbidden(client: TestClient) -> None:
    assert client.post(BASE, json={"name": "reload-plugins"}).json()["args"] == {}
    r = client.post(BASE, json={"name": "compact", "bogus": 1})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"
    assert client.post(BASE, json={"name": "compact", "args": "x"}).status_code == 422


def test_requires_auth(stub: StubManager) -> None:
    cfg = Config.model_validate({"server": {"auth_token": TOKEN}})
    bare = TestClient(create_app(stub, cfg))
    assert bare.post(BASE, json={"name": "compact"}).status_code == 401
    assert bare.get(BASE).status_code == 401


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"name": "mcp", "args": {"action": "reconnect"}}, 403, "command_not_allowed"),
        ({"name": "compact", "args": {"nope": 1}}, 422, "invalid_command"),
        ({"name": "reload-plugins", "args": {"force": True}}, 422, "invalid_command"),
        ({"name": "nonsense"}, 422, "invalid_command"),
    ],
)
def test_rejections(client: TestClient, body: dict, status: int, code: str) -> None:
    r = client.post(BASE, json=body)
    assert r.status_code == status and r.json()["error"]["code"] == code


def test_queue_full_429(client: TestClient, stub: StubManager) -> None:
    stub.router_obj.commands.cfg.client = ["compact", "reload-plugins", "reload-skills", "context"]
    for n in ("compact", "reload-plugins", "reload-skills", "context"):
        assert client.post(BASE, json={"name": n}).status_code == 202
    stub.router_obj.commands.cfg.client.append("mcp")
    r = client.post(BASE, json={"name": "mcp", "args": {"action": "reconnect"}})
    assert r.status_code == 429 and r.json()["error"]["code"] == "command_queue_full"


def test_not_running_409(client: TestClient, stub: StubManager) -> None:
    stub.router_obj.state = "busy"
    r = client.post(BASE, json={"name": "compact"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "dead"
    stub.stopped = True
    r = client.post(BASE, json={"name": "compact"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "dead"


def test_unknown_session_404(client: TestClient) -> None:
    r = client.post("/v1/sessions/nope/commands", json={"name": "compact"})
    assert r.status_code == 404
    assert client.get("/v1/sessions/nope/commands").status_code == 404


def test_list_and_get(client: TestClient, stub: StubManager) -> None:
    a = client.post(BASE, json={"name": "compact"}).json()
    b = client.post(BASE, json={"name": "reload-plugins"}).json()
    listed = client.get(BASE).json()["commands"]
    assert [c["id"] for c in listed] == [a["id"], b["id"]]
    rec = stub.router_obj.commands.get(a["id"])
    stub.router_obj.commands.start(rec)
    stub.router_obj.commands.finish(rec, "completed", result="done")
    got = client.get(f"{BASE}/{a['id']}").json()
    assert got["state"] == "completed" and got["result"] == "done"


def test_get_unknown_command_404(client: TestClient) -> None:
    r = client.get(f"{BASE}/cmd_missing")
    assert r.status_code == 404 and r.json()["error"]["code"] == "command_not_found"
