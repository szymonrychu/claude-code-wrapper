"""Tests for the peer wire models and the signing HTTP client (design D1, D5, D6, D13, D14)."""

from __future__ import annotations

import json
import re

import httpx
import pytest
from pydantic import ValidationError

from claude_code_wrapper import peer_auth
from claude_code_wrapper.peer_client import (
    RECEIVER_STATUS,
    MemberView,
    MessageBody,
    NoticeBody,
    PeerClient,
    PeerError,
    new_msg_id,
)

TOKEN = "s3cret-peer-token-value"
URL = "http://peer-b:8080"
ADDR = "alpha/ses_0123456789ab"
SID = "ses_0123456789ab"
MID = "msg_0123456789abcdef"


def _msg(**kw):
    args = {"from": ADDR, "to_session": SID, "message": "hi", "msg_id": MID}
    args.update(kw)
    return MessageBody.model_validate(args)


def _notice(**kw):
    args = {
        "from": ADDR,
        "to_session": SID,
        "msg_id": MID,
        "status": "delivered",
        "state": "completed",
    }
    args.update(kw)
    return NoticeBody.model_validate(args)


def _view(**kw):
    args = {
        "instance": "alpha",
        "url": "http://alpha:8080",
        "sessions": [{"session_id": SID, "name": "main", "cwd": "/w"}],
        "members": [{"instance": "beta", "url": "http://beta:8080"}],
    }
    args.update(kw)
    return MemberView.model_validate(args)


def _client(handler, captured):
    def wrapped(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    return PeerClient(TOKEN, transport=httpx.MockTransport(wrapped))


def _ok(status=202, payload=None):
    return lambda request: httpx.Response(status, json=payload or {"status": "queued"})


def _err(status, code="x"):
    return lambda request: httpx.Response(status, json={"error": {"code": code, "message": "m"}})


# --- models ---------------------------------------------------------------


def test_message_body_alias_and_dump():
    body = _msg()
    assert body.from_ == ADDR
    dumped = body.model_dump(by_alias=True)
    assert dumped["from"] == ADDR and "from_" not in dumped
    assert body.notify_when_idle is False


@pytest.mark.parametrize(
    "kw",
    [
        {"from": "nope"},
        {"from": "a/ses_xyz"},
        {"from": ".hidden/ses_0123456789ab"},
        {"to_session": "ses_XYZ"},
        {"msg_id": "msg_123"},
        {"message": ""},
        {"extra": 1},
    ],
)
def test_message_body_rejects(kw):
    with pytest.raises(ValidationError):
        _msg(**kw)


def test_notice_status_state_combinations():
    assert _notice().state == "completed"
    assert _notice(status="rejected", state=None, reason="no").reason == "no"
    assert _notice(status="dropped", state=None).state is None
    for kw in [
        {"status": "delivered", "state": None},
        {"status": "delivered", "state": "running"},
        {"status": "rejected", "state": "completed"},
        {"status": "dropped", "state": "failed"},
        {"status": "weird", "state": None},
        {"status": "rejected", "state": None, "reason": "x" * 201},
    ]:
        with pytest.raises(ValidationError):
            _notice(**kw)
    assert _notice(status="rejected", state=None, reason="x" * 200)


def test_wire_models_validate():
    assert _msg().model_dump(by_alias=True)["from"] == ADDR
    with pytest.raises(ValidationError):
        _msg(**{"from": "nope"})
    with pytest.raises(ValidationError):
        _msg(to_session="ses_XYZ")
    with pytest.raises(ValidationError):
        _msg(msg_id="msg_123")
    with pytest.raises(ValidationError):
        _notice(status="delivered", state=None)
    with pytest.raises(ValidationError):
        _notice(status="rejected", state="completed")
    assert _notice().model_dump(by_alias=True)["from"] == ADDR


def test_notice_alias():
    assert _notice().model_dump(by_alias=True)["from"] == ADDR


def test_member_view_valid_and_leaving():
    view = _view()
    assert view.leaving is False
    leave = _view(leaving=True, sessions=[])
    assert leave.leaving is True and leave.sessions == []


@pytest.mark.parametrize(
    "kw",
    [
        {"instance": ".bad"},
        {"instance": "a b"},
        {"url": "ftp://x"},
        {"url": "nope"},
        {"members": [{"instance": "..", "url": "http://x"}]},
        {"members": [{"instance": "b", "url": "bad"}]},
        {"sessions": [{"session_id": "bad", "name": "n", "cwd": "/"}]},
        {"extra": 1},
    ],
)
def test_member_view_rejects(kw):
    with pytest.raises(ValidationError):
        _view(**kw)


def test_member_view_caps():
    ok_members = [{"instance": f"m{i}", "url": f"http://m{i}:1"} for i in range(1024)]
    assert _view(members=ok_members)
    with pytest.raises(ValidationError):
        _view(members=[*ok_members, {"instance": "x", "url": "http://x:1"}])
    sessions = [{"session_id": f"ses_{i:012x}", "name": "n", "cwd": "/"} for i in range(1025)]
    with pytest.raises(ValidationError):
        _view(sessions=sessions)


def test_new_msg_id():
    a, b = new_msg_id(), new_msg_id()
    assert re.fullmatch(r"msg_[0-9a-f]{16}", a) and a != b


def test_receiver_status():
    assert RECEIVER_STATUS == {
        "not_found": 404,
        "inbox_full": 429,
        "rate_limited": 429,
        "left": 409,
    }


# --- client ---------------------------------------------------------------


def _verify(request: httpx.Request, cache=None):
    peer_auth.verify_request(
        TOKEN,
        request.method,
        request.url.path,
        dict(request.headers),
        request.content,
        cache or peer_auth.NonceCache(60),
        skew_seconds=60,
    )


async def test_requests_are_signed():
    cap = []
    client = _client(_ok(), cap)
    out = await client.send_message(URL, _msg())
    assert out == {"status": "queued"}
    (req,) = cap
    assert req.method == "POST" and str(req.url) == f"{URL}/v1/peers/messages"
    _verify(req)
    assert "authorization" not in req.headers
    data = json.loads(req.content)
    assert data["from"] == ADDR and "from_" not in data and data["msg_id"] == MID
    cap.clear()
    await client.send_notice(URL, _notice())
    (nreq,) = cap
    assert str(nreq.url) == f"{URL}/v1/peers/notices" and "authorization" not in nreq.headers
    _verify(nreq)
    await client.aclose()


async def test_send_notice_signed():
    cap = []
    client = _client(_ok(), cap)
    await client.send_notice(URL, _notice())
    (req,) = cap
    assert str(req.url) == f"{URL}/v1/peers/notices"
    _verify(req)
    assert json.loads(req.content)["from"] == ADDR
    await client.aclose()


async def test_base_url_trailing_slash():
    cap = []
    client = _client(_ok(), cap)
    await client.send_message(URL + "/", _msg())
    assert str(cap[0].url) == f"{URL}/v1/peers/messages"


@pytest.mark.parametrize("leaving", [False, True])
async def test_membership_calls(leaving):
    cap = []
    view = _view(leaving=True, sessions=[]) if leaving else _view()
    answer = view
    client = _client(lambda r: httpx.Response(200, json=answer.model_dump(by_alias=True)), cap)
    got = await client.heartbeat(URL, view)
    assert isinstance(got, MemberView) and got.leaving is leaving
    (req,) = cap
    assert str(req.url) == f"{URL}/v1/peers/heartbeat"
    _verify(req)
    assert json.loads(req.content)["leaving"] is leaving


async def test_heartbeat_not_retried():
    cap = []

    def boom(request):
        raise httpx.ConnectError("down")

    client = _client(boom, cap)
    with pytest.raises(PeerError) as ei:
        await client.heartbeat(URL, _view())
    assert ei.value.code == "unreachable" and len(cap) == 1


async def test_heartbeat_bad_answer_is_unreachable():
    client = _client(lambda r: httpx.Response(200, json={"junk": 1}), [])
    with pytest.raises(PeerError) as ei:
        await client.heartbeat(URL, _view())
    assert ei.value.code == "unreachable"


def test_no_join_or_leave_call():
    assert not hasattr(PeerClient, "join") and not hasattr(PeerClient, "leave")


@pytest.mark.parametrize(
    ("status", "code", "prefix", "expected"),
    [
        (404, "not_found", "unknown peer", "unknown_peer"),
        (413, "too_large", "message too large", "too_large"),
        (429, "inbox_full", "inbox full", "inbox_full"),
        (429, "rate_limited", "rate limited", "rate_limited"),
        (429, "whatever", "rate limited", "rate_limited"),
        (401, "unauthorized", "peer rejected the request", "unauthorized"),
        (422, "invalid", "invalid arguments", "invalid"),
        (500, "boom", "peer unreachable", "unreachable"),
        (503, "boom", "peer unreachable", "unreachable"),
    ],
)
async def test_status_mapping(status, code, prefix, expected):
    cap = []
    client = _client(_err(status, code), cap)
    with pytest.raises(PeerError) as ei:
        await client.send_message(URL, _msg())
    assert ei.value.code == expected
    assert ei.value.message.startswith(prefix)
    assert len(cap) == 1  # an HTTP answer is never retried


async def test_non_json_error_body():
    client = _client(lambda r: httpx.Response(429, text="slow down"), [])
    with pytest.raises(PeerError) as ei:
        await client.send_notice(URL, _notice())
    assert ei.value.code == "rate_limited"


async def test_retry_once_on_connection_failure():
    cap = []
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("refused")
        return httpx.Response(202, json={"status": "queued"})

    client = _client(flaky, cap)
    assert await client.send_message(URL, _msg()) == {"status": "queued"}
    assert len(cap) == 2
    a, b = cap
    assert json.loads(a.content)["msg_id"] == json.loads(b.content)["msg_id"] == MID
    assert a.headers[peer_auth.HEADER_NONCE] != b.headers[peer_auth.HEADER_NONCE]
    cache = peer_auth.NonceCache(60)
    _verify(a, cache)
    _verify(b, cache)


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("x"),
        httpx.ConnectTimeout("x"),
        httpx.ReadTimeout("x"),
        httpx.RemoteProtocolError("x"),
        httpx.ReadError("x"),
        httpx.WriteError("x"),
    ],
)
async def test_unreachable_after_retry(exc):
    cap = []

    def boom(request):
        raise exc

    client = _client(boom, cap)
    with pytest.raises(PeerError) as ei:
        await client.send_message(URL, _msg())
    assert ei.value.code == "unreachable"
    assert URL in ei.value.message and ei.value.message.startswith("peer unreachable")
    assert len(cap) == 2


async def test_token_never_on_the_wire():
    cap = []
    client = _client(_err(500), cap)
    with pytest.raises(PeerError) as ei:
        await client.send_message(URL, _msg())
    for req in cap:
        blob = str(req.url) + str(dict(req.headers)) + req.content.decode()
        assert TOKEN not in blob
    assert TOKEN not in ei.value.message and TOKEN not in str(ei.value)

    def boom(request):
        raise httpx.ConnectError(f"leaky {TOKEN}")

    client = _client(boom, [])
    with pytest.raises(PeerError) as ei2:
        await client.send_message(URL, _msg())
    assert TOKEN not in ei2.value.message


def test_peer_error_attrs():
    e = PeerError("not_found", "no such session")
    assert e.code == "not_found" and e.message == "no such session"
