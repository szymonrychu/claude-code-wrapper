"""Three real ccw services on loopback ports: seed join, gossip, messages, notices, leave."""

from __future__ import annotations

import asyncio
import json
import re
import socket
from typing import Any

import httpx
from conftest import jsonl

PEER_TOKEN = "peer-shared-token-0123456789"
SCENARIO = {
    "turns": [
        {
            "match": r"(?m)^send (?P<body>ping) to (?P<to>\S+)$",
            "steps": [
                {
                    "mcp": {
                        "server": "ccw",
                        "tool": "send_message",
                        "arguments": {
                            "to": "{{to}}",
                            "message": "{{body}}",
                            "notify_when_idle": True,
                        },
                    }
                }
            ],
        },
        {
            "match": (
                r'<agent-message from="(?P<from>[^"]+)" msg_id="[^"]+">'
                r"\nping\n</agent-message>"
            ),
            "steps": [
                {
                    "mcp": {
                        "server": "ccw",
                        "tool": "send_message",
                        "arguments": {"to": "{{from}}", "message": "pong"},
                    }
                }
            ],
        },
        {
            "match": r"(?m)^list peers$",
            "steps": [{"mcp": {"server": "ccw", "tool": "peers", "arguments": {}}}],
        },
    ]
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Node:
    """One service with its session; reads its disk state."""

    def __init__(self, world, name: str, service, sid: str) -> None:
        self.world, self.name, self.svc, self.sid = world, name, service, sid
        self.root = world.tmp / name
        self.headers = {"Authorization": f"Bearer tok-{name}-0123456789"}
        self.address = f"{name}/{sid}"

    def events(self, type_: str | None = None) -> list[dict]:
        rows = jsonl(self.root / "sessions" / self.sid / "events.jsonl")
        return [e for e in rows if type_ is None or e.get("type") == type_]

    def tool_calls(self) -> list[dict]:
        """One entry per MCP tool call: {tool, arguments, text}."""
        path = self.root / "sessions" / self.sid / "config" / "fake-mcp.jsonl"
        out = []
        for line in jsonl(path) if path.exists() else []:
            call = next(r for r in line["requests"] if r.get("method") == "tools/call")
            answer = line["responses"][-1] or {}
            res = answer.get("result") or {}
            text = "".join(c.get("text", "") for c in res.get("content") or [])
            if "error" in answer:
                text = str(answer["error"].get("message", answer["error"]))
            out.append(
                {
                    "tool": call["params"]["name"],
                    "arguments": call["params"]["arguments"],
                    "text": text,
                }
            )
        return out

    def sent(self, to: str) -> list[dict]:
        return [
            c
            for c in self.tool_calls()
            if c["tool"] == "send_message" and c["arguments"]["to"] == to
        ]

    def received(self) -> list[dict]:
        return self.events("peer_message_received")

    async def submit(self, prompt: str) -> None:
        """POST a client turn, retrying while the session is busy (bounded at 30 s)."""
        end = asyncio.get_running_loop().time() + 30
        async with httpx.AsyncClient() as c:
            while True:
                r = await c.post(
                    f"{self.svc.url}/v1/sessions/{self.sid}/turns",
                    json={"text": prompt},
                    headers=self.headers,
                )
                if r.status_code != 409 or asyncio.get_running_loop().time() > end:
                    break
                await asyncio.sleep(0.1)
        assert r.status_code == 202, f"{self.name} turn {prompt!r}: {r.text}"

    async def turns(self) -> list[dict]:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self.svc.url}/v1/sessions/{self.sid}/turns", headers=self.headers)
        return r.json()["turns"]

    async def metrics(self) -> str:
        async with httpx.AsyncClient() as c:
            return (await c.get(f"{self.svc.url}/metrics")).text

    def list_results(self) -> list[list[dict]]:
        return [json.loads(c["text"]) for c in self.tool_calls() if c["tool"] == "peers"]


async def list_peers(node: Node) -> list[str]:
    """Run `list peers` on a node and return the addresses of its newest result."""
    before = len(node.list_results())
    await node.submit("list peers")
    await until(lambda: len(node.list_results()) > before, f"{node.name} peers result")
    return sorted(row["address"] for row in node.list_results()[-1])


async def until(pred, what: str, timeout: float = 30.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.1)


async def eventually_listing(node: Node, expect: list[str], what: str, timeout: float = 30.0):
    end = asyncio.get_running_loop().time() + timeout
    got: list[str] = []
    while asyncio.get_running_loop().time() < end:
        got = await list_peers(node)
        if got == sorted(expect):
            return
        await asyncio.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}: last listing {got}, wanted {expect}")


def peers_cfg(name: str, port: int, static: list[str], **over: Any) -> dict[str, Any]:
    return {
        "enabled": True,
        "name": name,
        "advertise_url": f"http://127.0.0.1:{port}",
        "token": PEER_TOKEN,
        "static": static,
        "heartbeat_seconds": 0.3,
        "max_missed_heartbeats": 2,
        "join_timeout_seconds": 2,
        **over,
    }


async def boot(world, ccw_service, **peer_over: Any) -> dict[str, Node]:
    ports = {n: free_port() for n in "abc"}
    seeds = {
        "a": [],
        "b": [f"http://127.0.0.1:{ports['a']}"],
        "c": [f"http://127.0.0.1:{ports['b']}"],
    }
    nodes: dict[str, Node] = {}
    for n in "abc":
        cfg = world.service_cfg(
            n,
            SCENARIO,
            server={"port": ports[n]},
            peers=peers_cfg(n, ports[n], seeds[n], **peer_over),
        )
        svc = await ccw_service(cfg)
        assert svc.public_port == ports[n]
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{svc.url}/v1/sessions",
                json={},
                headers={"Authorization": f"Bearer tok-{n}-0123456789"},
            )
        assert r.status_code < 300, r.text
        nodes[n] = Node(world, n, svc, r.json()["session_id"])
    return nodes


def metric(text: str, name: str, **labels: str) -> float:
    """The value of one labelled sample; 0.0 when the sample is absent."""
    want = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    for m in re.finditer(rf"^{name}\{{([^}}]*)\}} (\S+)$", text, re.M):
        if ",".join(sorted(m.group(1).split(","))) == want:
            return float(m.group(2))
    return 0.0


async def peer_turns(node: Node) -> list[str]:
    """The prompt text of every peer-origin turn on a node, oldest first."""
    out = []
    async with httpx.AsyncClient() as c:
        for t in await node.turns():
            if t["origin"] != "peer":
                continue
            r = await c.get(
                f"{node.svc.url}/v1/sessions/{node.sid}/turns/{t['turn_id']}",
                headers=node.headers,
            )
            out.append(r.json()["text"])
    return out


async def exchange(src: Node, dst: Node) -> None:
    """src sends ping to dst; dst replies pong; src gets the idle notice; then 2 s quiet."""
    await src.submit(f"send ping to {dst.address}")
    await until(lambda: any(e["data"]["message"] == "ping" for e in dst.received()), "ping")
    await until(lambda: any(e["data"]["message"] == "pong" for e in src.received()), "pong")
    await until(lambda: any(e["data"]["kind"] == "notice" for e in src.received()), "idle notice")
    await until(lambda: len(src.events("peer_message_delivered")) >= 2, "src deliveries")
    await until(lambda: len(dst.events("peer_message_delivered")) >= 1, "dst delivery")
    await asyncio.sleep(2)


async def test_gossip_listing_across_services(world, ccw_service) -> None:
    nodes = await boot(world, ccw_service)
    a, b, c = nodes["a"], nodes["b"], nodes["c"]
    # C only knows B, yet A lists both through gossip, and never itself
    await eventually_listing(a, [b.address, c.address], "A to list the B and C sessions")
    assert a.address not in await list_peers(a)
    joined = {e["data"]["instance"] for e in a.events("peer_member_joined")}
    assert {"b", "c"} <= joined


async def test_send_and_reply_round_trip(world, ccw_service) -> None:
    nodes = await boot(world, ccw_service)
    a, b, c = nodes["a"], nodes["b"], nodes["c"]
    await eventually_listing(a, [b.address, c.address], "A to list B and C")
    await exchange(a, c)
    ping = [e for e in c.received() if e["data"]["kind"] == "message"]
    assert [(e["data"]["from"], e["data"]["message"]) for e in ping] == [(a.address, "ping")]
    pong = [e for e in a.received() if e["data"]["kind"] == "message"]
    assert [(e["data"]["from"], e["data"]["message"]) for e in pong] == [(c.address, "pong")]
    assert [x["arguments"]["message"] for x in a.sent(c.address)] == ["ping"]
    assert [x["arguments"]["message"] for x in c.sent(a.address)] == ["pong"]
    assert a.sent(c.address)[0]["text"].startswith("Sent msg_")
    c_peer = await peer_turns(c)
    assert len(c_peer) == 1
    assert c_peer[0].startswith(f'<agent-message from="{a.address}" msg_id="msg_')
    assert "\nping\n</agent-message>" in c_peer[0]
    assert any(
        t.startswith(f'<agent-message from="{c.address}"') and "\npong\n" in t
        for t in await peer_turns(a)
    )
    assert not await peer_turns(b)  # B took no part


async def test_reverse_direction(world, ccw_service) -> None:
    nodes = await boot(world, ccw_service)
    a, b, c = nodes["a"], nodes["b"], nodes["c"]
    await eventually_listing(c, [a.address, b.address], "C to list A and B")
    await exchange(c, a)
    ping = [e for e in a.received() if e["data"]["kind"] == "message"]
    assert [(e["data"]["from"], e["data"]["message"]) for e in ping] == [(c.address, "ping")]
    pong = [e for e in c.received() if e["data"]["kind"] == "message"]
    assert [(e["data"]["from"], e["data"]["message"]) for e in pong] == [(a.address, "pong")]
    assert [x["arguments"]["message"] for x in c.sent(a.address)] == ["ping"]
    assert [x["arguments"]["message"] for x in a.sent(c.address)] == ["pong"]
    a_peer = await peer_turns(a)
    assert a_peer and a_peer[0].startswith(f'<agent-message from="{c.address}" msg_id="msg_')
    assert "\nping\n</agent-message>" in a_peer[0]


async def test_notify_when_idle_round_trip(world, ccw_service) -> None:
    nodes = await boot(world, ccw_service)
    a, b, c = nodes["a"], nodes["b"], nodes["c"]
    await eventually_listing(a, [b.address, c.address], "A to list B and C")
    await exchange(a, c)
    notices = [e for e in a.received() if e["data"]["kind"] == "notice"]
    assert [e["data"]["from"] for e in notices] == [c.address]
    frames = [t for t in await peer_turns(a) if t.startswith("<peer-idle-notice")]
    assert len(frames) == 1
    assert frames[0].startswith(f'<peer-idle-notice from="{c.address}" msg_id="msg_')
    assert 'state="completed"/>' in frames[0].splitlines()[0]
    assert not [t for t in await peer_turns(a) if t.startswith("<peer-delivery-failed")]
    delivered = [e for e in a.events("peer_message_delivered") if e["data"]["kind"] == "notice"]
    assert [e["data"]["result"] for e in delivered] == ["delivered"]


async def test_events_and_metrics(world, ccw_service) -> None:
    nodes = await boot(world, ccw_service)
    a, b, c = nodes["a"], nodes["b"], nodes["c"]
    await eventually_listing(a, [b.address, c.address], "A to list B and C")
    await exchange(a, c)
    assert [e["data"]["kind"] for e in a.events("peer_message_sent")] == ["message"]
    assert sorted(e["data"]["kind"] for e in c.events("peer_message_sent")) == [
        "message",
        "notice",
    ]
    assert [
        (e["data"]["kind"], e["data"]["result"]) for e in c.events("peer_message_delivered")
    ] == [("message", "delivered")]
    assert sorted(
        (e["data"]["kind"], e["data"]["result"]) for e in a.events("peer_message_delivered")
    ) == [("message", "delivered"), ("notice", "delivered")]
    assert sorted(e["data"]["kind"] for e in a.received()) == ["message", "notice"]
    assert [e["data"]["kind"] for e in c.received()] == ["message"]
    assert not b.received() and not b.events("peer_message_sent")
    ma, mb, mc = await a.metrics(), await b.metrics(), await c.metrics()
    n = "ccw_peer_messages"
    assert metric(ma, f"{n}_sent_total", kind="message", result="ok") == 1
    assert metric(mc, f"{n}_sent_total", kind="message", result="ok") == 1
    assert metric(mc, f"{n}_sent_total", kind="notice", result="ok") == 1
    assert metric(ma, f"{n}_received_total", kind="message", result="accepted") == 1
    assert metric(ma, f"{n}_received_total", kind="notice", result="accepted") == 1
    assert metric(mc, f"{n}_received_total", kind="message", result="accepted") == 1
    assert metric(ma, f"{n}_delivered_total", kind="message", result="delivered") == 1
    assert metric(ma, f"{n}_delivered_total", kind="notice", result="delivered") == 1
    assert metric(mc, f"{n}_delivered_total", kind="message", result="delivered") == 1
    assert metric(mb, f"{n}_received_total", kind="message", result="accepted") == 0


async def test_stopped_service_leaves(world, ccw_service) -> None:
    # a miss budget far beyond the 5 s wait, so only the leaving heartbeat can drop C
    nodes = await boot(world, ccw_service, max_missed_heartbeats=100)
    a, b, c = nodes["a"], nodes["b"], nodes["c"]
    await eventually_listing(a, [b.address, c.address], "A to list B and C")
    # A drops C within about a heartbeat, via the leaving heartbeat
    await c.svc.stop()
    await eventually_listing(a, [b.address], "A to drop C after it stopped", timeout=5)
    left = [e for e in a.events("peer_member_left") if e["data"]["instance"] == "c"]
    assert left and left[-1]["data"]["reason"] == "leave"


async def test_unknown_peer_and_unsigned_heartbeat(world, ccw_service) -> None:
    nodes = await boot(world, ccw_service)
    a, b = nodes["a"], nodes["b"]
    await a.submit("send ping to b/ses_000000000000")
    await until(lambda: a.sent("b/ses_000000000000"), "A unknown-peer result")
    assert a.sent("b/ses_000000000000")[-1]["text"].startswith("unknown peer")
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{b.svc.url}/v1/peers/heartbeat",
            json={},
            headers={"Authorization": f"Bearer {PEER_TOKEN}"},
        )
        assert r.status_code == 401
        for route in ("join", "leave"):
            r = await client.post(f"{b.svc.url}/v1/peers/{route}", json={}, headers=b.headers)
            assert r.status_code == 404
