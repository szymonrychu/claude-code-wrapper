"""GET /v1/peers: the bearer-protected peer mesh view, mounted whether or not peers are enabled."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.config import Config
from claude_code_wrapper.peers import MemberTable, PeerSession
from claude_code_wrapper.redact import Redactor

PEER_TOKEN = "peer-token-0123456789abcdef"
SERVER_TOKEN = "server-token-0123456789"
AUTH = {"Authorization": f"Bearer {SERVER_TOKEN}"}


class Service:
    instance = "me"
    url = "http://me:8080"

    def __init__(self, table: MemberTable) -> None:
        self.table = table


class Manager:
    started = True

    def __init__(self, peers=None) -> None:
        self.redactor = Redactor()
        self.peers = peers

    def list(self, state=None):
        return []


def client(manager: Manager, enabled: bool) -> TestClient:
    cfg = Config.model_validate(
        {
            "server": {"auth_token": SERVER_TOKEN},
            "peers": {"enabled": enabled, "name": "me", "token": PEER_TOKEN},
        }
    )
    return TestClient(create_app(manager, cfg))


def test_disabled_returns_enabled_false() -> None:
    r = client(Manager(), enabled=False).get("/v1/peers", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"enabled": False}


def test_enabled_lists_members() -> None:
    now = [100.0]
    table = MemberTable("me", "http://me:8080", max_missed=3, clock=lambda: now[0])
    table.record_report("b", "http://b:8080", [PeerSession("ses_1", "n", "/w")])
    table.add_contacts(["http://c:8080"])
    table.record_report("c", "http://c:8080", [])
    table.record_failure("http://c:8080")
    now[0] = 130.0
    c = client(Manager(Service(table)), enabled=True)
    body = c.get("/v1/peers", headers=AUTH).json()
    assert body["enabled"] is True
    assert body["self"] == {"instance": "me", "url": "http://me:8080"}
    members = {m["instance"]: m for m in body["members"]}
    assert set(members) == {"b", "c"}
    b = members["b"]
    assert b["url"] == "http://b:8080"
    assert b["sessions"] == [{"session_id": "ses_1", "name": "n", "cwd": "/w"}]
    assert b["missed"] == 0
    assert "reachable" not in b
    assert members["c"]["missed"] == 1
    seen = datetime.fromisoformat(b["last_seen_at"])
    assert seen.tzinfo is not None
    age = datetime.now(seen.tzinfo) - seen
    assert timedelta(seconds=29) < age < timedelta(seconds=40)


def test_requires_bearer_and_hides_token() -> None:
    table = MemberTable("me", "http://me:8080", max_missed=3)
    table.record_report("b", "http://b:8080", [])
    c = client(Manager(Service(table)), enabled=True)
    assert c.get("/v1/peers").status_code == 401
    assert c.get("/v1/peers", headers={"Authorization": "Bearer nope"}).status_code == 401
    r = c.get("/v1/peers", headers=AUTH)
    assert r.status_code == 200
    assert PEER_TOKEN not in r.text
    assert "token" not in r.json()
    # unauthenticated requests are refused when peers are disabled too
    assert client(Manager(), enabled=False).get("/v1/peers").status_code == 401
