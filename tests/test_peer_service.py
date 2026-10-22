"""PeerService: membership, signed sending, receiving and notices for every session (D1, D4,
D7, D8, D9). Two services talk through an in-process mesh that routes each url to the other's
real `create_app`, so every request is signed and verified end to end."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from test_peer_inbox import PeerStub, _value, until
from test_router import CLAUDE_SID, Harness, webhook

from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.config import Config
from claude_code_wrapper.hooks import HookDispatcher
from claude_code_wrapper.metrics import Metrics
from claude_code_wrapper.peer_client import MessageBody, NoticeBody, PeerError
from claude_code_wrapper.peer_inbox import InboxItem, PeerInbox
from claude_code_wrapper.peer_service import PeerService
from claude_code_wrapper.redact import Redactor
from claude_code_wrapper.router import SessionInfo

TOKEN = "peer-token-0123456789abcdef"
SERVER_TOKEN = "server-token-0123456789"
A_URL = "http://a:8080"
B_URL = "http://b:8080"
A1, A2 = "ses_aaaaaaaaaa01", "ses_aaaaaaaaaa02"
B1, B2, B3 = "ses_bbbbbbbbbb01", "ses_bbbbbbbbbb02", "ses_bbbbbbbbbb03"


class Mesh(httpx.AsyncBaseTransport):
    """Routes `scheme://host:port` to an in-process ASGI app; records every request."""

    def __init__(self) -> None:
        self.apps: dict[str, httpx.ASGITransport] = {}
        self.down: set[str] = set()
        #: Seconds to hold a request before it reaches the app (cancellable), if any.
        self.delay: Callable[[httpx.Request], float] | None = None
        self.requests: list[httpx.Request] = []

    def add(self, url: str, app: Any) -> None:
        self.apps[url] = httpx.ASGITransport(app=app)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        base = f"{request.url.scheme}://{request.url.host}:{request.url.port}"
        if base in self.down or base not in self.apps:
            raise httpx.ConnectError("connection refused", request=request)
        if self.delay is not None:
            await asyncio.sleep(self.delay(request))
        return await self.apps[base].handle_async_request(request)


class Sess(Harness):
    """A test_router stub session with its own session id and a PeerStub (busy on submit)."""

    def __init__(self, root: Path, sid: str, *, hooks: dict | None = None, reply=None) -> None:
        root.mkdir(parents=True)
        super().__init__(root, hooks=hooks)
        if reply is not None:
            self.reply = reply
        self.sid = sid
        self.router.info = SessionInfo(sid, CLAUDE_SID, str(self.workdir), "http://session.cb/x")
        self.session = PeerStub(self.workdir, self.transcript)
        self.router.bind(self.session)  # type: ignore[arg-type]

    def data(self, type: str) -> list[dict]:
        return [e.data for e in self.events(type)]

    def notices(self) -> list[dict]:
        return [d for d in self.data("peer_message_received") if d["kind"] == "notice"]

    async def finish(self, state: str = "completed"):
        """Finish the running stub turn through the Stop relay."""
        record = self.session.turn
        record.state = state
        self.session.complete_result = record
        await self.router.handle_claude_hook("Stop", {})
        self.session.finish(self.router)
        return record


class Node:
    def __init__(self, root: Path, mesh: Mesh, name: str, advertise: str, **peers: Any) -> None:
        self.root = root / name
        self.root.mkdir()
        self.cfg = Config.model_validate(
            {
                "server": {"auth_token": SERVER_TOKEN},
                "peers": {
                    "enabled": True,
                    "name": name,
                    "advertise_url": advertise,
                    "token": TOKEN,
                    "heartbeat_seconds": 30,
                    **peers,
                },
            }
        )
        self.redactor = Redactor()
        self.metrics = Metrics()
        dispatcher = HookDispatcher(
            self.cfg.hooks,
            self.redactor,
            base_env={"PATH": os.environ["PATH"]},
            default_cwd=self.root,
        )
        self.svc = PeerService(
            self.cfg.peers,
            public_port=8080,
            redactor=self.redactor,
            dispatcher=dispatcher,
            metrics=self.metrics,
            transport=mesh,
        )
        manager = SimpleNamespace(redactor=self.redactor, peers=self.svc, started=True)
        mesh.add(self.svc.url, create_app(manager, self.cfg, self.metrics))
        self.sessions: dict[str, Sess] = {}

    def add(self, sid: str, *, busy: bool = False, **kw: Any) -> Sess:
        s = Sess(self.root / sid, sid, **kw)
        if busy:
            s.session.state = "busy"
        inbox = self.svc.register(sid, s.router, name=f"name-{sid[-2:]}", cwd=f"/w/{sid}")
        assert isinstance(inbox, PeerInbox) and s.router.peer_inbox is inbox
        self.sessions[sid] = s
        return s

    async def drop(self, sid: str) -> None:
        """Unregister a session, then close its router (the manager's order)."""
        s = self.sessions.pop(sid)
        self.svc.unregister(sid)
        await s.router.close()

    def sent(self, kind: str, result: str) -> float:
        sample = f'ccw_peer_messages_sent_total{{kind="{kind}",result="{result}"}}'
        return _value(self.metrics, sample) or 0.0

    async def close(self) -> None:
        await self.svc.leave()
        for sid in list(self.sessions):
            await self.drop(sid)
        await self.svc.stop(1.0)


class Net:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.mesh = Mesh()
        self.nodes: list[Node] = []

    def node(self, name: str, advertise: str, **peers: Any) -> Node:
        n = Node(self.root, self.mesh, name, advertise, **peers)
        self.nodes.append(n)
        return n

    async def pair(self, a_sessions=(A1,), b_sessions=(B1,), **b_peers: Any):
        """A (no seeds) joins, then B seeded with A joins."""
        a = self.node("a", "http://a:{port}")
        b = self.node("b", B_URL, static=[A_URL], **b_peers)
        for sid in a_sessions:
            a.add(sid)
        for sid in b_sessions:
            b.add(sid)
        await a.svc.join()
        await b.svc.join()
        return a, b


@pytest.fixture
async def net(tmp_path):
    n = Net(tmp_path)
    yield n
    for node in n.nodes:
        await node.close()


def addresses(rows: list[dict]) -> list[str]:
    return [r["address"] for r in rows]


# ---------------------------------------------------------------- membership


async def test_membership_listing(net):
    a, b = await net.pair()
    assert a.svc.instance == "a" and a.svc.url == A_URL
    assert b.svc.instance == "b" and b.svc.url == B_URL
    # Each lists the other's sessions (with name and cwd), never the caller itself.
    assert await a.svc.list_peers(A1, None) == [
        {
            "address": f"b/{B1}",
            "instance": "b",
            "session_id": B1,
            "name": "name-01",
            "cwd": f"/w/{B1}",
        }
    ]
    assert addresses(await b.svc.list_peers(B1, None)) == [f"a/{A1}"]
    # A newly registered session reaches B after the immediate heartbeat.
    a.add(A2)

    async def b_sees_a2() -> bool:
        return f"a/{A2}" in addresses(await b.svc.list_peers(B1, None))

    deadline = time.monotonic() + 5
    while not await b_sees_a2():
        assert time.monotonic() < deadline, "B never learned A's new session"
        await asyncio.sleep(0.01)
    # Our other sessions are listed too, sorted by address, and the instance filter works.
    assert addresses(await a.svc.list_peers(A1, None)) == [f"a/{A2}", f"b/{B1}"]
    assert addresses(await a.svc.list_peers(A1, "b")) == [f"b/{B1}"]
    assert addresses(await a.svc.list_peers(A1, "a")) == [f"a/{A2}"]
    assert await a.svc.list_peers(A1, "nope") == []
    # Unregistering leaves the local listing at once and B's after the next round.
    a.svc.unregister(A2)
    assert addresses(await a.svc.list_peers(A1, None)) == [f"b/{B1}"]
    deadline = time.monotonic() + 5
    while await b_sees_a2():
        assert time.monotonic() < deadline, "B never dropped A's unregistered session"
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------- sending


async def test_send_reaches_target_inbox(net):
    a, b = await net.pair(a_sessions=(A1, A2))
    a1, a2, b1 = a.sessions[A1], a.sessions[A2], b.sessions[B1]
    # Session-local: through our own signed route, into the local target's inbox.
    local = await a.svc.send(A1, f"a/{A2}", "hi neighbour", False)
    assert local.startswith("msg_") and len(local) == 20
    await until(lambda: a2.data("peer_message_received"))
    (got,) = a2.data("peer_message_received")
    assert got["msg_id"] == local and got["from"] == f"a/{A1}" and got["kind"] == "message"
    assert got["message"] == "hi neighbour"
    await until(lambda: a2.session.calls)
    assert "hi neighbour" in a2.calls("submit")[0][1]
    # Cross-instance: B's inbox gets it, with notify_when_idle carried along.
    remote = await a.svc.send(A1, f"b/{B1}", "hello b", True)
    await until(lambda: b1.data("peer_message_received"))
    (got,) = b1.data("peer_message_received")
    assert got["msg_id"] == remote and got["from"] == f"a/{A1}" and got["message"] == "hello b"
    await until(lambda: b1.session.calls)
    assert b1.router.peer_inbox.delivered_turn(b1.session.turn.turn_id).notify_when_idle
    # The sender's stream gets peer_message_sent with result ok for both.
    assert a1.data("peer_message_sent") == [
        {
            "msg_id": local,
            "to": f"a/{A2}",
            "kind": "message",
            "bytes": len(b"hi neighbour"),
            "notify_when_idle": False,
            "result": "ok",
        },
        {
            "msg_id": remote,
            "to": f"b/{B1}",
            "kind": "message",
            "bytes": len(b"hello b"),
            "notify_when_idle": True,
            "result": "ok",
        },
    ]
    assert a.sent("message", "ok") == 2


async def test_send_failures(net):
    a, b = await net.pair(
        a_sessions=(A1, A2),
        b_sessions=(),
        inbox_max=3,
        max_messages_per_minute=2,
        max_message_bytes=64,
    )
    b.add(B1, busy=True)
    b.add(B2)
    # B's new sessions reach A after the immediate heartbeat.
    deadline = time.monotonic() + 5
    while len(await a.svc.list_peers(A1, "b")) < 2:
        assert time.monotonic() < deadline
        await asyncio.sleep(0.01)
    a1 = a.sessions[A1]

    async def fails(code: str, caller: str, to: str, message: str = "x") -> PeerError:
        with pytest.raises(PeerError) as info:
            await a.svc.send(caller, to, message, False)
        assert info.value.code == code, info.value.message
        return info.value

    # Bad address and self address: invalid.
    err = await fails("invalid", A1, "not-an-address")
    assert err.message.startswith("invalid arguments")
    await fails("invalid", A1, "../ses_bbbbbbbbbb01")
    await fails("invalid", A1, f"a/{A1}")
    # Too large: UTF-8 bytes, not characters, against our own max_message_bytes.
    limit = a.cfg.peers.max_message_bytes
    err = await fails("too_large", A1, f"b/{B1}", "\u00e9" * (limit // 2 + 1))
    assert err.message.startswith("message too large")
    # ... and the receiver's own limit (B takes 64 bytes) maps to too_large as well.
    await fails("too_large", A1, f"b/{B2}", "x" * 100)
    # Unknown peer: an unknown instance, an unknown session of a member, an unknown local one.
    err = await fails("unknown_peer", A1, "c/ses_cccccccccccc")
    assert err.message.startswith("unknown peer")
    await fails("unknown_peer", A1, "b/ses_ffffffffffff")
    await fails("unknown_peer", A1, "a/ses_ffffffffffff")
    # Rate limited: B allows two messages a minute from one sender to one session.
    await a.svc.send(A1, f"b/{B1}", "one", False)
    await a.svc.send(A1, f"b/{B1}", "two", False)
    err = await fails("rate_limited", A1, f"b/{B1}", "three")
    assert err.message.startswith("rate limited")
    # Inbox full: B1 (busy) holds three items; another sender's third is refused.
    await a.svc.send(A2, f"b/{B1}", "from a2", False)
    err = await fails("inbox_full", A2, f"b/{B1}", "from a2 again")
    assert err.message.startswith("inbox full")
    # Unreachable: B stops answering (the client retries once, then gives up).
    net.mesh.down.add(B_URL)
    before = len(net.mesh.requests)
    err = await fails("unreachable", A1, f"b/{B2}")
    assert err.message.startswith("peer unreachable")
    assert len(net.mesh.requests) - before == 2
    # Every failure is counted, and every attempt is on the caller's stream.
    assert a.sent("message", "invalid") == 3
    assert a.sent("message", "too_large") == 2
    assert a.sent("message", "unknown_peer") == 3
    assert a.sent("message", "rate_limited") == 1
    assert a.sent("message", "inbox_full") == 1
    assert a.sent("message", "unreachable") == 1
    assert a.sent("message", "ok") == 3
    results = [d["result"] for d in a1.data("peer_message_sent")]
    assert results.count("invalid") == 3 and results.count("too_large") == 2
    assert results.count("unknown_peer") == 3 and results.count("unreachable") == 1
    assert results.count("ok") == 2 and results.count("rate_limited") == 1
    assert [d["result"] for d in a.sessions[A2].data("peer_message_sent")] == ["ok", "inbox_full"]


async def test_accept_unknown_session(net):
    a, b = await net.pair()
    body = MessageBody.model_validate(
        {
            "from": f"a/{A1}",
            "to_session": "ses_ffffffffffff",
            "message": "hi",
            "msg_id": "msg_0123456789abcdef",
        }
    )
    with pytest.raises(PeerError) as info:
        await b.svc.accept_message(body)
    assert info.value.code == "not_found"
    notice = NoticeBody.model_validate(
        {
            "from": f"a/{A1}",
            "to_session": "ses_ffffffffffff",
            "msg_id": "msg_0123456789abcdef",
            "status": "dropped",
        }
    )
    with pytest.raises(PeerError) as info:
        await b.svc.accept_notice(notice)
    assert info.value.code == "not_found"
    # A known session accepts, reports the queue depth and dedupes a retry.
    known = body.model_copy(update={"to_session": B1})
    assert await b.svc.accept_message(known) == {
        "msg_id": body.msg_id,
        "queued": 1,
        "duplicate": False,
    }
    assert (await b.svc.accept_message(known))["duplicate"] is True
    # A closing session (inbox closed) is not found either.
    await b.drop(B1)
    other = known.model_copy(update={"msg_id": "msg_1123456789abcdef"})
    with pytest.raises(PeerError) as info:
        await b.svc.accept_message(other)
    assert info.value.code == "not_found"


# ---------------------------------------------------------------- notices


async def test_notices_round_trip(net):
    deny = {"decision": "deny", "reason": "not now"}
    a = net.node("a", "http://a:{port}")
    b = net.node("b", B_URL, static=[A_URL])
    a1 = a.add(A1, busy=True)  # notices queue in the sender's inbox and stay visible
    b1 = b.add(B1)
    b.add(B2, hooks={"onTurn": [webhook()]}, reply=deny)
    b3 = b.add(B3, busy=True)
    await a.svc.join()
    await b.svc.join()

    # A plain delivered message: no notice.
    plain = await a.svc.send(A1, f"b/{B1}", "plain", False)
    await until(lambda: b1.session.turn is not None)
    await b1.finish()
    # Delivered with notify_when_idle: one `delivered` notice once the handling turn ends.
    wished = await a.svc.send(A1, f"b/{B1}", "tell me", True)
    await until(lambda: b1.session.turn is not None)
    await asyncio.sleep(0.05)
    assert a1.notices() == []
    await b1.finish()
    await until(lambda: len(a1.notices()) == 1)
    # Rejected by B2's onTurn hook: one `rejected` notice.
    vetoed = await a.svc.send(A1, f"b/{B2}", "veto me", False)
    await until(lambda: len(a1.notices()) == 2)
    # Still queued when B3 goes away: one `dropped` notice.
    dropped = await a.svc.send(A1, f"b/{B3}", "drop me", False)
    await until(lambda: b3.data("peer_message_received"))
    await b.drop(B3)
    await until(lambda: len(a1.notices()) == 3)
    await asyncio.sleep(0.1)  # no stragglers or duplicates

    notices = a1.notices()
    assert [n["msg_id"] for n in notices] == [wished, vetoed, dropped]
    assert {n["from"] for n in notices} == {f"b/{B1}", f"b/{B2}", f"b/{B3}"}
    assert plain not in [n["msg_id"] for n in notices]
    queued = list(a1.router.peer_inbox._items)  # what the agent will see
    assert [(i.from_address, i.status, i.state, i.reason) for i in queued] == [
        (f"b/{B1}", "delivered", "completed", None),
        (f"b/{B2}", "rejected", None, "not now"),
        (f"b/{B3}", "dropped", None, "session closed"),
    ]
    # Each notice is counted and on the receiving session's stream.
    assert b.sent("notice", "ok") == 3
    for s, msg_id in ((b1, wished), (b.sessions[B2], vetoed)):
        (ev,) = [d for d in s.data("peer_message_sent") if d["kind"] == "notice"]
        assert ev["msg_id"] == msg_id and ev["to"] == f"a/{A1}" and ev["result"] == "ok"

    # An unknown sender or an unreachable one is logged and counted, never raised.
    b1.session.state = "busy"
    inbox = b1.router.peer_inbox
    inbox.put(
        InboxItem(
            kind="message", from_address="c/ses_cccccccccccc", msg_id="msg_" + "1" * 16, text="x"
        )
    )
    inbox.put(InboxItem(kind="message", from_address=f"a/{A1}", msg_id="msg_" + "2" * 16, text="y"))
    net.mesh.down.add(A_URL)
    await b.drop(B1)
    await b.svc.stop(2.0)
    assert b.sent("notice", "unknown_peer") == 1
    assert b.sent("notice", "unreachable") == 1
    assert len(a1.notices()) == 3


# ---------------------------------------------------------------- member events


async def test_member_events(net):
    a = net.node("a", "http://a:{port}")
    b = net.node("b", B_URL, static=[A_URL])
    a1, a2 = a.add(A1), a.add(A2)
    b1 = b.add(B1)
    await a.svc.join()
    await b.svc.join()
    joined_b = {"instance": "b", "url": B_URL, "reason": "heartbeat"}
    for s in (a1, a2):
        assert s.data("peer_member_joined") == [joined_b]
    assert b1.data("peer_member_joined") == [{"instance": "a", "url": A_URL, "reason": "join"}]
    # B leaves (and its container goes away): every registered session on A sees it at once.
    await b.svc.leave()
    net.mesh.down.add(B_URL)
    left_b = {"instance": "b", "url": B_URL, "reason": "leave"}
    for s in (a1, a2):
        assert s.data("peer_member_left") == [left_b]
    # An unregistered session hears nothing more.
    a.svc.unregister(A2)
    b2 = net.node("b2", "http://b2:8080", static=[A_URL])
    await b2.svc.join()
    assert a1.data("peer_member_joined")[-1]["instance"] == "b2"
    assert len(a2.data("peer_member_joined")) == 1
    await a.sessions.pop(A2).router.close()


# ---------------------------------------------------------------- stop


async def test_stop_drains(net):
    a, b = await net.pair(b_sessions=(B1, B2))
    a1 = a.sessions[A1]
    a1.session.state = "busy"
    b1, b2 = b.sessions[B1], b.sessions[B2]
    b1.session.state = b2.session.state = "busy"
    first = await a.svc.send(A1, f"b/{B1}", "pending one", False)
    second = await a.svc.send(A1, f"b/{B2}", "pending two", False)
    await until(lambda: b1.data("peer_message_received") and b2.data("peer_message_received"))
    await b.svc.leave()  # the manager's order: leave, sessions down, then stop
    # A answers the first dropped notice slowly and never answers the second.
    net.mesh.delay = lambda req: 3600.0 if second.encode() in req.content else 0.3
    await b.drop(B1)
    await b.drop(B2)
    await asyncio.sleep(0.05)
    assert a1.notices() == []
    t0 = time.monotonic()
    await b.svc.stop(1.0)
    elapsed = time.monotonic() - t0
    assert 0.9 <= elapsed < 2.0, elapsed
    # The slow notice landed within the timeout; the hung one was given up.
    assert [n["msg_id"] for n in a1.notices()] == [first]
    assert b.svc.client._http.is_closed


# ---------------------------------------------------------------- token


async def test_token_never_exposed(net, caplog):
    caplog.set_level(logging.DEBUG)
    a, b = await net.pair(a_sessions=(A1, A2))
    assert a.redactor.redact(f"x {TOKEN} y") == "x *** y"
    await a.svc.send(A1, f"a/{A2}", "local", True)
    await a.svc.send(A1, f"b/{B1}", "remote", True)
    for bad in ("nope", f"a/{A1}", "c/ses_cccccccccccc"):
        with pytest.raises(PeerError) as info:
            await a.svc.send(A1, bad, "x", False)
        assert TOKEN not in info.value.message
    b1 = b.sessions[B1]
    await until(lambda: b1.session.turn is not None)
    net.mesh.down.add(A_URL)
    with pytest.raises(PeerError) as info:
        await b.svc.send(B1, f"a/{A1}", "unreachable", False)
    assert TOKEN not in info.value.message
    await b1.finish()  # its delivered notice fails: logged, redacted
    await b.svc.stop(1.0)
    net.mesh.down.clear()
    await a.svc.leave()

    assert net.mesh.requests
    for req in net.mesh.requests:
        assert TOKEN not in str(req.url)
        assert all(TOKEN not in v for v in req.headers.values())
        assert TOKEN.encode() not in req.content
    for node in net.nodes:
        for s in node.sessions.values():
            dumped = json.dumps([e.data for e in s.events()])
            assert TOKEN not in dumped
            assert TOKEN not in s.log.path.read_text()
    assert "delivered notice for" in caplog.text  # the failure was logged ...
    assert TOKEN not in caplog.text  # ... without the token
