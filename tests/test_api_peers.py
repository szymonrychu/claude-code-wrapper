"""Receiving peer routes: HMAC-authenticated, body-capped, mounted only when enabled."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from claude_code_wrapper import peer_auth
from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.config import Config
from claude_code_wrapper.peer_client import PeerError
from claude_code_wrapper.redact import Redactor

PEER_TOKEN = "peer-token-0123456789abcdef"
SERVER_TOKEN = "server-token-0123456789"
ROUTES = ("messages", "notices", "heartbeat")

MESSAGE = {
    "from": "a/ses_0123456789ab",
    "to_session": "ses_ba9876543210",
    "message": "hello",
    "msg_id": "msg_0123456789abcdef",
}
NOTICE = {
    "from": "a/ses_0123456789ab",
    "to_session": "ses_ba9876543210",
    "msg_id": "msg_0123456789abcdef",
    "status": "delivered",
    "state": "completed",
}
VIEW = {"instance": "a", "url": "http://a:8080", "sessions": [], "members": []}
BODIES = {"messages": MESSAGE, "notices": NOTICE, "heartbeat": VIEW}


class FakeReceiver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.error: PeerError | None = None

    async def accept_message(self, body):
        self.calls.append(("message", body))
        if self.error:
            raise self.error
        return {"msg_id": body.msg_id, "queued": True}

    async def accept_notice(self, body):
        self.calls.append(("notice", body))
        if self.error:
            raise self.error
        return {"msg_id": body.msg_id, "queued": True, "duplicate": True}

    async def accept_heartbeat(self, view):
        self.calls.append(("heartbeat", view))
        if self.error:
            raise self.error
        from claude_code_wrapper.peer_client import MemberView

        return MemberView(instance="me", url="http://me:8080")


class StubManager:
    started = True

    def __init__(self) -> None:
        self.redactor = Redactor()
        self.peers = FakeReceiver()

    def list(self, state=None):
        return []


def make(enabled: bool = True, **peers):
    cfg = Config.model_validate(
        {
            "server": {"auth_token": SERVER_TOKEN},
            "peers": {
                "enabled": enabled,
                "name": "me",
                "advertise_url": "http://me:8080",
                "token": PEER_TOKEN,
                **peers,
            },
        }
    )
    mgr = StubManager()
    return TestClient(create_app(mgr, cfg)), mgr


def post(c, route, body=None, *, headers=None, raw=None, token=PEER_TOKEN, path=None):
    raw = json.dumps(BODIES[route] if body is None else body).encode() if raw is None else raw
    path = path or f"/v1/peers/{route}"
    h = {"Content-Type": "application/json"}
    h.update(peer_auth.sign_request(token, "POST", path, raw) if headers is None else headers)
    return c.post(path, content=raw, headers=h)


@pytest.mark.parametrize("route,status", [("messages", 202), ("notices", 202), ("heartbeat", 200)])
def test_accepts_signed_requests(route, status) -> None:
    c, mgr = make()
    r = post(c, route)
    assert r.status_code == status, r.text
    assert len(mgr.peers.calls) == 1
    if route == "heartbeat":
        assert r.json()["instance"] == "me"
    if route == "messages":
        assert r.json() == {"msg_id": MESSAGE["msg_id"], "queued": True}
    if route == "notices":
        assert r.json()["duplicate"] is True


def test_leaving_heartbeat() -> None:
    c, mgr = make()
    r = post(c, "heartbeat", {**VIEW, "leaving": True})
    assert r.status_code == 200
    assert mgr.peers.calls[0][1].leaving is True


@pytest.mark.parametrize("route", ["join", "leave"])
def test_no_join_leave_routes(route) -> None:
    c, _ = make()
    raw = b"{}"
    r = c.post(
        f"/v1/peers/{route}",
        content=raw,
        headers=peer_auth.sign_request(PEER_TOKEN, "POST", f"/v1/peers/{route}", raw),
    )
    assert r.status_code == 404


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("case", ["none", "badsig", "changed", "wrongtoken"])
def test_bad_signature_401(route, case) -> None:
    c, mgr = make()
    raw = json.dumps(BODIES[route]).encode()
    path = f"/v1/peers/{route}"
    if case == "none":
        r = post(c, route, headers={})
    elif case == "badsig":
        h = peer_auth.sign_request(PEER_TOKEN, "POST", path, raw)
        h[peer_auth.HEADER_SIGNATURE] = "0" * 64
        r = post(c, route, headers=h)
    elif case == "changed":
        h = peer_auth.sign_request(PEER_TOKEN, "POST", path, raw)
        r = post(c, route, raw=raw + b" ", headers=h)
    else:
        r = post(c, route, token="other-token")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"
    assert PEER_TOKEN not in r.text
    assert mgr.peers.calls == []


@pytest.mark.parametrize("route", ROUTES)
def test_stale_timestamp_401(route) -> None:
    c, mgr = make()
    raw = json.dumps(BODIES[route]).encode()
    h = peer_auth.sign_request(PEER_TOKEN, "POST", f"/v1/peers/{route}", raw, now_ms=1000)
    r = post(c, route, headers=h)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"
    assert mgr.peers.calls == []


@pytest.mark.parametrize("route", ROUTES)
def test_replayed_nonce_401(route) -> None:
    c, mgr = make()
    raw = json.dumps(BODIES[route]).encode()
    h = peer_auth.sign_request(PEER_TOKEN, "POST", f"/v1/peers/{route}", raw)
    assert post(c, route, headers=h).status_code in (200, 202)
    mgr.peers.calls.clear()
    r = post(c, route, headers=h)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"
    assert mgr.peers.calls == []


class RecordingMetrics:
    def __init__(self) -> None:
        self.received: list[tuple[str, str]] = []
        self.membership: list[tuple[str, str]] = []

    def peer_received(self, kind, result):
        self.received.append((kind, result))

    def peer_membership(self, op, result):
        self.membership.append((op, result))

    def __getattr__(self, name):
        return lambda *a, **k: None


def _metric_client():
    c, mgr = make(max_message_bytes=100)
    rec = RecordingMetrics()
    c.app.state.metrics = rec
    return c, mgr, rec


def test_route_metrics() -> None:
    c, mgr, rec = _metric_client()
    post(c, "messages", headers={})
    post(c, "notices", headers={})
    post(c, "messages", raw=b"x" * 5000)
    post(c, "notices", raw=b"x" * 5000)
    post(c, "messages", {**MESSAGE, "message": "x" * 101})
    mgr.peers.error = PeerError("not_found", "nope")
    post(c, "messages")
    post(c, "notices")
    assert rec.received == [
        ("message", "unauthorized"),
        ("notice", "unauthorized"),
        ("message", "too_large"),
        ("notice", "too_large"),
        ("message", "too_large"),
        ("message", "not_found"),
        ("notice", "not_found"),
    ]
    assert rec.membership == []


def test_heartbeat_metrics() -> None:
    c, mgr, rec = _metric_client()
    assert post(c, "heartbeat").status_code == 200
    assert post(c, "heartbeat", headers={}).status_code == 401
    mgr.peers.error = PeerError("rate_limited", "slow")
    assert post(c, "heartbeat").status_code == 429
    assert rec.membership == [
        ("inbound_heartbeat", "ok"),
        ("inbound_heartbeat", "failed"),
        ("inbound_heartbeat", "failed"),
    ]
    assert rec.received == []


def test_heartbeat_metrics_other_failures() -> None:
    c, _, rec = _metric_client()
    assert post(c, "heartbeat", raw=b"x" * (1024 * 1024 + 4097)).status_code == 413
    assert post(c, "heartbeat", {"bogus": 1}).status_code == 422
    c.app.state.peer_nonces = peer_auth.NonceCache(60, capacity=1)
    assert post(c, "heartbeat").status_code == 200
    assert post(c, "heartbeat").status_code == 429
    assert rec.membership == [("inbound_heartbeat", "failed")] * 2 + [
        ("inbound_heartbeat", "ok"),
        ("inbound_heartbeat", "failed"),
    ]


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("token", [SERVER_TOKEN, PEER_TOKEN])
def test_bearer_tokens_rejected(route, token) -> None:
    c, mgr = make()
    raw = json.dumps(BODIES[route]).encode()
    r = c.post(
        f"/v1/peers/{route}",
        content=raw,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    assert r.status_code == 401
    assert mgr.peers.calls == []


def test_peer_bearer_not_accepted_on_v1() -> None:
    c, _ = make()
    r = c.get("/v1/sessions", headers={"Authorization": f"Bearer {PEER_TOKEN}"})
    assert r.status_code == 401


@pytest.mark.parametrize("route", ROUTES)
def test_body_cap_before_parse(route) -> None:
    c, mgr = make(max_message_bytes=100)
    raw = b"x" * (100 + 4096 + 1)
    r = post(c, route, raw=raw)
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "message_too_large"

    def gen():
        yield b"x" * 3000
        yield b"x" * 3000

    h = peer_auth.sign_request(PEER_TOKEN, "POST", f"/v1/peers/{route}", b"x" * 6000)
    r = c.post(f"/v1/peers/{route}", content=gen(), headers=h)  # chunked, no Content-Length
    assert r.status_code == 413
    assert mgr.peers.calls == []


def test_oversize_message_utf8() -> None:
    c, mgr = make(max_message_bytes=10)
    r = post(c, "messages", {**MESSAGE, "message": "é" * 6})  # 12 bytes, 6 chars
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "message_too_large"
    assert mgr.peers.calls == []
    assert post(c, "messages", {**MESSAGE, "message": "é" * 5}).status_code == 202


@pytest.mark.parametrize(
    "code,status", [("not_found", 404), ("inbox_full", 429), ("rate_limited", 429)]
)
@pytest.mark.parametrize("route", ROUTES)
def test_error_statuses(route, code, status) -> None:
    c, mgr = make()
    mgr.peers.error = PeerError(code, "nope")
    r = post(c, route)
    assert r.status_code == status
    assert r.json()["error"]["code"] == code


@pytest.mark.parametrize("route", ROUTES)
def test_malformed_422(route) -> None:
    c, mgr = make()
    r = post(c, route, {"bogus": 1})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"
    assert mgr.peers.calls == []


def test_nonce_cache_full_429() -> None:
    c, _ = make()
    from claude_code_wrapper import peer_auth as pa

    c.app.state.peer_nonces = pa.NonceCache(60, capacity=1)
    assert post(c, "heartbeat").status_code == 200
    r = post(c, "heartbeat")
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"


@pytest.mark.parametrize("route", ROUTES)
def test_routes_absent_when_disabled(route) -> None:
    c, _ = make(enabled=False)
    assert post(c, route).status_code == 404


def test_peer_token_redacted() -> None:
    _, mgr = make()
    assert PEER_TOKEN not in mgr.redactor.redact(f"x {PEER_TOKEN} y")


def test_peer_routes_documented() -> None:
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    api = re.search(r"^## API reference\s*$(.*?)^## ", readme, re.S | re.M).group(1)
    for route in ROUTES:
        assert f"### `POST /v1/peers/{route}`" in api
    errors = re.search(r"^## Errors\s*$(.*?)^## ", readme, re.S | re.M).group(1)
    for code in ("message_too_large", "inbox_full", "rate_limited"):
        assert re.search(rf"^\|\s*`{code}`\s*\|", errors, re.M), code
