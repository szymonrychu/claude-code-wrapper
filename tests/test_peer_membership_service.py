"""Peer membership runtime: join, heartbeat and leave with the four instance hooks."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pytest

from claude_code_wrapper.config import Config
from claude_code_wrapper.hooks import instance_dispatcher
from claude_code_wrapper.metrics import Metrics
from claude_code_wrapper.peer_client import RECEIVER_STATUS, MemberView, PeerClient, PeerError
from claude_code_wrapper.peer_membership import MembershipService
from claude_code_wrapper.peers import Member, MemberTable, PeerSession
from claude_code_wrapper.redact import Redactor

TOKEN = "peer-token-0123456789"
A = "http://a:1"
B = "http://b:1"
C = "http://c:1"
SID_A = "ses_aaaaaaaaaaaa"
SID_B = "ses_bbbbbbbbbbbb"
SID_C = "ses_cccccccccccc"

Route = Callable[[httpx.Request, MemberView], Awaitable[httpx.Response]]


class Net:
    """Routes peer urls to handlers; appends every heartbeat to the shared order log."""

    def __init__(self, log: Path) -> None:
        self.log = log
        self.routes: dict[str, Route] = {}
        self.calls: list[tuple[str, MemberView]] = []

    def to(self, url: str) -> list[MemberView]:
        return [view for base, view in self.calls if base == url]

    def transport(self) -> httpx.MockTransport:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/peers/heartbeat"
            assert TOKEN not in request.content.decode()
            assert all(TOKEN not in v for v in request.headers.values())
            base = f"{request.url.scheme}://{request.url.host}:{request.url.port}"
            view = MemberView.model_validate_json(request.content)
            self.calls.append((base, view))
            with self.log.open("a") as f:
                f.write(f"hb {base} {view.instance} {'leaving' if view.leaving else 'beat'}\n")
            route = self.routes.get(base)
            if route is None:
                raise httpx.ConnectError("connection refused", request=request)
            return await route(request, view)

        return httpx.MockTransport(handler)


class Stub:
    """A stub peer answering heartbeats with a fixed view; `mode` switches its behaviour."""

    def __init__(self, instance: str, url: str, sid: str, members: list[dict] | None = None):
        self.instance = instance
        self.url = url
        self.sid = sid
        self.members = members or []
        self.mode = "ok"  # ok | down | hang | slow | refuse
        self.down_calls = 0

    async def __call__(self, request: httpx.Request, view: MemberView) -> httpx.Response:
        if self.mode == "down":
            self.down_calls += 1
            raise httpx.ConnectError("connection refused", request=request)
        if self.mode == "hang":
            await asyncio.sleep(3600)
        if self.mode == "slow":
            await asyncio.sleep(0.2)
        if self.mode == "refuse":
            return httpx.Response(503, json={"error": {"code": "unavailable", "message": "no"}})
        return httpx.Response(
            200,
            json={
                "instance": self.instance,
                "url": self.url,
                "sessions": [{"session_id": self.sid, "name": self.instance, "cwd": "/w"}],
                "members": self.members,
                "leaving": False,
            },
        )


def service_route(svc: MembershipService) -> Route:
    """Route a url to another MembershipService's receiver method, as the endpoint does."""

    async def route(request: httpx.Request, view: MemberView) -> httpx.Response:
        try:
            answer = await svc.accept_heartbeat(view)
        except PeerError as e:
            return httpx.Response(
                RECEIVER_STATUS.get(e.code, 500),
                json={"error": {"code": e.code, "message": e.message}},
            )
        return httpx.Response(200, json=answer.model_dump(by_alias=True))

    return route


def rec(log: Path, out: Path, event: str, **kw) -> dict:
    """A blocking command action saving its payload and appending its event to the log."""
    return {
        "type": "command",
        "run": f"cat > {out / (event + '.json')}; echo {event} >> {log}",
        "blocking": True,
        **kw,
    }


def recording_hooks(log: Path, out: Path) -> dict:
    return {e: [rec(log, out, e)] for e in ("preJoin", "postJoin", "preLeave", "postLeave")}


def payload(out: Path, event: str) -> dict:
    return json.loads((out / (event + ".json")).read_text())["data"]


def lines(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def metric(m: Metrics, sample: str) -> float | None:
    for line in m.render().decode().splitlines():
        if line.startswith(sample + " "):
            return float(line.rsplit(" ", 1)[1])
    return None


def membership(m: Metrics, op: str, result: str) -> float | None:
    return metric(m, f'ccw_peer_membership_total{{op="{op}",result="{result}"}}')


async def until(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.log = tmp_path / "order.log"
        self.out = tmp_path / "payloads"
        self.out.mkdir()
        self.net = Net(self.log)
        self.made: list[tuple[MembershipService, PeerClient]] = []
        self.changes: dict[str, list[tuple[str, str, str]]] = {}

    def build(
        self,
        instance: str = "a",
        url: str = A,
        *,
        static: list[str] | None = None,
        hooks: dict | None = None,
        sessions: list[PeerSession] | None = None,
        metrics: Metrics | None = None,
        capacity: int = 1024,
        on_change: Callable[[str, Member, str], None] | None = None,
        **peers,
    ) -> MembershipService:
        peers_cfg = {
            "enabled": True,
            "token": TOKEN,
            "static": static or [],
            "heartbeat_seconds": 0.05,
            "max_missed_heartbeats": 2,
            "join_timeout_seconds": 0.3,
            **peers,
        }
        cfg = Config.model_validate(
            {
                "paths": {"sessions_root": str(self.tmp / ("root-" + instance))},
                "hooks": hooks or {},
                "peers": peers_cfg,
            }
        )
        dispatcher = instance_dispatcher(
            cfg,
            Redactor([]),
            service_env={"PATH": os.environ["PATH"]},
            api_url="http://127.0.0.1:1",
        )
        client = PeerClient(TOKEN, transport=self.net.transport())
        table = MemberTable(
            instance, url, max_missed=cfg.peers.max_missed_heartbeats, capacity=capacity
        )
        own = sessions if sessions is not None else []
        events = self.changes.setdefault(instance, [])
        svc = MembershipService(
            cfg.peers,
            instance=instance,
            url=url,
            client=client,
            table=table,
            dispatcher=dispatcher,
            own_sessions=lambda: list(own),
            on_change=on_change
            or (lambda kind, member, reason: events.append((kind, member.instance, reason))),
            metrics=metrics,
        )
        self.made.append((svc, client))
        return svc


@pytest.fixture
async def env(tmp_path):
    e = Env(tmp_path)
    yield e
    for svc, client in e.made:
        await svc.leave()
        await client.aclose()


def names(svc: MembershipService) -> set[str]:
    return {m.instance for m in svc.table.reachable()}


# ---------------------------------------------------------------------------------- join


async def test_join_order_and_hooks(env):
    b = Stub("b", B, SID_B)
    env.net.routes[B] = b
    m = Metrics()
    svc = env.build(
        static=[B, "http://dead:1"],
        hooks=recording_hooks(env.log, env.out),
        sessions=[PeerSession(SID_A, "main", "/work")],
        metrics=m,
        heartbeat_seconds=30,
    )
    data = await svc.join()

    log = lines(env.log)
    assert log[0] == "preJoin"
    assert log[-1] == "postJoin"
    assert sorted(log[1:-1]) == sorted(["hb http://b:1 a beat", "hb http://dead:1 a beat"])

    assert payload(env.out, "preJoin") == {"instance": "a", "url": A, "members": []}
    assert payload(env.out, "postJoin") == data
    assert data["instance"] == "a"
    assert data["url"] == A
    assert data["members"] == [{"instance": "b", "url": B}]
    assert data["standalone"] is False
    assert [f["url"] for f in data["failures"]] == ["http://dead:1"]
    assert data["failures"][0]["error"]
    assert TOKEN not in json.dumps(data)

    sent = env.net.to(B)[0]
    assert [s.session_id for s in sent.sessions] == [SID_A]
    assert sent.leaving is False
    assert [s["address"] for s in svc.table.sessions()] == [f"b/{SID_B}"]
    assert env.changes["a"] == [("joined", "b", "join")]
    assert svc.standalone is False
    assert membership(m, "join", "ok") == 1
    assert membership(m, "join", "failed") == 1
    assert metric(m, "ccw_peer_members") == 1


async def test_prejoin_veto_standalone(env):
    env.net.routes[B] = Stub("b", B, SID_B)
    m = Metrics()
    hooks = recording_hooks(env.log, env.out)
    hooks["preJoin"] = [{"type": "command", "run": "exit 2", "blocking": True}]
    svc = env.build(static=[B], hooks=hooks, metrics=m)

    data = await svc.join()
    assert svc.standalone is True
    assert data["standalone"] is True
    assert data["failures"] == []
    assert payload(env.out, "postJoin") == data
    assert membership(m, "join", "vetoed") == 1

    svc.sessions_changed()
    await asyncio.sleep(0.3)  # six heartbeat intervals
    assert env.net.calls == []

    # a standalone instance still answers others
    answer = await svc.accept_heartbeat(MemberView(instance="b", url=B))
    assert answer.instance == "a"
    assert names(svc) == {"b"}

    left = await svc.leave()
    assert lines(env.log) == ["postJoin", "preLeave", "postLeave"]
    assert left["failures"] == []
    assert env.net.calls == []


async def test_unreachable_seed_bounded_then_retried(env):
    hang = Stub("h", "http://hang:1", SID_B)
    hang.mode = "hang"
    down = Stub("d", "http://down:1", SID_C)
    down.mode = "down"
    env.net.routes["http://hang:1"] = hang
    env.net.routes["http://down:1"] = down
    svc = env.build(static=["http://hang:1", "http://down:1"], join_timeout_seconds=0.3)

    started = time.monotonic()
    data = await svc.join()
    assert time.monotonic() - started < 0.3 + 0.5
    assert sorted(f["url"] for f in data["failures"]) == ["http://down:1", "http://hang:1"]
    assert data["members"] == []

    hang.mode = "ok"
    down.mode = "ok"
    await until(lambda: names(svc) == {"h", "d"})
    assert ("joined", "h", "heartbeat") in env.changes["a"]
    assert ("joined", "d", "heartbeat") in env.changes["a"]


# ---------------------------------------------------------------------------------- gossip


async def test_gossip_converges(env):
    a = env.build("a", A, sessions=[PeerSession(SID_A, "alpha", "/a")])
    b = env.build("b", B, static=[A], sessions=[PeerSession(SID_B, "beta", "/b")])
    c = env.build("c", C, static=[B], sessions=[PeerSession(SID_C, "gamma", "/c")])
    for svc, url in ((a, A), (b, B), (c, C)):
        env.net.routes[url] = service_route(svc)

    await a.join()
    await b.join()
    await c.join()
    await until(
        lambda: names(a) == {"b", "c"} and names(b) == {"a", "c"} and names(c) == {"a", "b"}
    )

    # sessions come from each member's own report
    assert {s["address"]: s["name"] for s in c.table.sessions()} == {
        f"a/{SID_A}": "alpha",
        f"b/{SID_B}": "beta",
    }
    assert {s["address"] for s in a.table.sessions()} == {f"b/{SID_B}", f"c/{SID_C}"}


# ---------------------------------------------------------------------------------- reachability


async def test_missed_heartbeats_leave_and_readd(env):
    b = Stub("b", B, SID_B)
    env.net.routes[B] = b
    m = Metrics()
    misses_at_drop: list[int] = []
    events: list[tuple[str, str, str]] = []

    def on_change(kind: str, member: Member, reason: str) -> None:
        events.append((kind, member.instance, reason))
        if kind == "left":
            misses_at_drop.append(b.down_calls)

    svc = env.build(static=[B], max_missed_heartbeats=2, metrics=m, on_change=on_change)
    await svc.join()
    assert names(svc) == {"b"}

    b.mode = "down"
    await until(lambda: ("left", "b", "missed_heartbeats") in events)
    assert misses_at_drop == [2]
    assert names(svc) == set()
    assert svc.table.lookup(f"b/{SID_B}") is None
    assert metric(m, "ccw_peer_members") == 0
    assert (membership(m, "heartbeat", "failed") or 0) >= 2

    b.mode = "ok"
    await until(lambda: ("joined", "b", "heartbeat") in events)
    assert names(svc) == {"b"}
    assert membership(m, "heartbeat", "ok")

    # a leaving heartbeat drops it at once
    b.mode = "refuse"
    answer = await svc.accept_heartbeat(MemberView(instance="b", url=B, leaving=True))
    assert answer.instance == "a"
    assert names(svc) == set()
    assert events[-1] == ("left", "b", "leave")
    assert events == [
        ("joined", "b", "join"),
        ("left", "b", "missed_heartbeats"),
        ("joined", "b", "heartbeat"),
        ("left", "b", "leave"),
    ]


async def test_accept_heartbeat_records_and_caps(env):
    svc = env.build(capacity=1)
    view = MemberView(
        instance="x",
        url="http://x:1",
        sessions=[{"session_id": SID_B, "name": "n", "cwd": "/x"}],
        members=[{"instance": "y", "url": "http://y:1"}],
    )
    answer = await svc.accept_heartbeat(view)
    assert answer.instance == "a"
    assert answer.url == A
    assert [mm.instance for mm in answer.members] == ["x"]  # the sender skips itself
    assert answer.leaving is False
    assert names(svc) == {"x"}
    assert env.changes["a"] == [("joined", "x", "heartbeat")]
    assert "http://y:1" not in svc.table.targets()  # contact set is capped at the table capacity

    with pytest.raises(PeerError) as exc:
        await svc.accept_heartbeat(MemberView(instance="z", url="http://z:1"))
    assert exc.value.code == "rate_limited"
    assert RECEIVER_STATUS[exc.value.code] == 429


# ---------------------------------------------------------------------------------- push


async def test_sessions_changed_pushes(env):
    b = Stub("b", B, SID_B)
    env.net.routes[B] = b
    own: list[PeerSession] = []
    svc = env.build(static=[B], sessions=own, heartbeat_seconds=30)
    await svc.join()
    assert len(env.net.to(B)) == 1

    b.mode = "slow"
    own.append(PeerSession(SID_A, "one", "/w"))
    assert svc.sessions_changed() is None
    await until(lambda: len(env.net.to(B)) == 2, timeout=1.0)  # well before the 30 s interval
    assert [s.session_id for s in env.net.to(B)[1].sessions] == [SID_A]

    # a burst while that round is in flight queues exactly one more round
    own.append(PeerSession("ses_000000000001", "two", "/w"))
    for _ in range(5):
        svc.sessions_changed()
    await until(lambda: len(env.net.to(B)) == 3, timeout=1.0)
    await asyncio.sleep(0.6)
    assert len(env.net.to(B)) == 3
    assert len(env.net.to(B)[2].sessions) == 2


# ---------------------------------------------------------------------------------- leave


async def test_leave_order_and_bound(env):
    b = Stub("b", B, SID_B)
    c = Stub("c", C, SID_C)
    env.net.routes[B] = b
    env.net.routes[C] = c
    m = Metrics()
    hooks = recording_hooks(env.log, env.out)
    hooks["preLeave"] = [
        {
            "type": "command",
            "run": f"cat > {env.out / 'preLeave.json'}; echo preLeave >> {env.log}; sleep 0.3",
            "blocking": True,
        }
    ]
    svc = env.build(
        static=[B, C],
        hooks=hooks,
        sessions=[PeerSession(SID_A, "main", "/w")],
        metrics=m,
        heartbeat_seconds=0.02,
        max_missed_heartbeats=1000,
        join_timeout_seconds=0.3,
    )
    await svc.join()
    await until(lambda: len(env.net.to(B)) >= 3)
    c.mode = "hang"

    started = time.monotonic()
    data = await svc.leave()
    assert time.monotonic() - started < 0.3 + 0.3 + 0.6

    log = lines(env.log)
    i = log.index("preLeave")
    assert sorted(log[i + 1 : -1]) == ["hb http://b:1 a leaving", "hb http://c:1 a leaving"]
    assert log[-1] == "postLeave"
    assert "preLeave" not in log[:i]

    for url in (B, C):
        final = env.net.to(url)[-1]
        assert final.leaving is True
        assert final.sessions == []

    assert payload(env.out, "postLeave") == data
    assert set(payload(env.out, "preLeave")) == {"instance", "url", "members"}
    assert sorted(mm["instance"] for mm in data["members"]) == ["b", "c"]
    assert [f["url"] for f in data["failures"]] == [C]
    assert membership(m, "leave", "ok") == 1
    assert membership(m, "leave", "failed") == 1

    # the loop is stopped: nothing more goes out, and a second leave returns at once
    before = len(env.net.calls)
    svc.sessions_changed()
    await asyncio.sleep(0.2)
    assert len(env.net.calls) == before
    started = time.monotonic()
    again = await svc.leave()
    assert time.monotonic() - started < 0.05
    assert again == data
    assert lines(env.log) == log


async def test_heartbeat_after_leave_still_answered(env):
    svc = env.build()
    await svc.join()
    await svc.leave()
    # after leave an inbound heartbeat is still recorded and answered, like any other
    view = MemberView(
        instance="b", url=B, sessions=[{"session_id": SID_B, "name": "b", "cwd": "/w"}]
    )
    answer = await svc.accept_heartbeat(view)
    assert answer.instance == "a"
    assert names(svc) == {"b"}
    # a leaving peer is still taken out
    answer = await svc.accept_heartbeat(MemberView(instance="b", url=B, leaving=True))
    assert answer.instance == "a"
    assert names(svc) == set()


# ---------------------------------------------------------------------------------- loop


async def test_loop_never_blocked(env):
    hang = Stub("h", "http://hang:1", SID_B)
    hang.mode = "hang"
    live = Stub("l", "http://live:1", SID_C)
    env.net.routes["http://hang:1"] = hang
    env.net.routes["http://live:1"] = live
    svc = env.build(
        static=["http://hang:1", "http://live:1"],
        heartbeat_seconds=0.05,
        max_missed_heartbeats=1000,
        join_timeout_seconds=0.3,
    )

    ticks: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:
        while not stop.is_set():
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    task = asyncio.create_task(ticker())
    try:
        started = time.monotonic()
        await svc.join()
        assert time.monotonic() - started < 0.3 + 0.5
        for _ in range(20):
            svc.sessions_changed()
        await svc.accept_heartbeat(MemberView(instance="x", url="http://x:1"))
        await asyncio.sleep(0.3)
        live.mode = "hang"  # a reachable member that now hangs
        started = time.monotonic()
        await svc.leave()
        assert time.monotonic() - started < 0.3 + 0.5
    finally:
        stop.set()
        await task
    gaps = [b - a for a, b in zip(ticks, ticks[1:], strict=False)]
    assert len(ticks) > 50
    assert max(gaps) < 0.2
