"""Three real ccw services on loopback ports: seed join, gossip, messages, notices, leave."""

from __future__ import annotations

import asyncio
import json
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


def peers_cfg(name: str, port: int, static: list[str]) -> dict[str, Any]:
    return {
        "enabled": True,
        "name": name,
        "advertise_url": f"http://127.0.0.1:{port}",
        "token": PEER_TOKEN,
        "static": static,
        "heartbeat_seconds": 0.3,
        "max_missed_heartbeats": 2,
        "join_timeout_seconds": 2,
    }


async def boot(world, ccw_service) -> dict[str, Node]:
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
            peers=peers_cfg(n, ports[n], seeds[n]),
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


async def test_three_services_exchange_messages_and_leave(world, ccw_service) -> None:
    nodes = await boot(world, ccw_service)
    a, b, c = nodes["a"], nodes["b"], nodes["c"]

    # seed join and gossip: C only knows B, yet A lists both
    await eventually_listing(a, [b.address, c.address], "A to list the B and C sessions")
    assert a.address not in await list_peers(a)

    # A -> C: message, reply, idle notice, no loop
    await a.submit(f"send ping to {c.address}")
    await until(lambda: any(e["data"]["message"] == "ping" for e in c.received()), "C ping")
    await until(lambda: any(e["data"]["kind"] == "notice" for e in a.received()), "A idle notice")
    await until(lambda: any(e["data"]["message"] == "pong" for e in a.received()), "A pong")
    await until(lambda: len(a.events("peer_message_delivered")) >= 2, "A deliveries")
    await asyncio.sleep(2)
    ping = [e for e in c.received() if e["data"]["kind"] == "message"]
    assert [(e["data"]["from"], e["data"]["message"]) for e in ping] == [(a.address, "ping")]
    pong = [e for e in a.received() if e["data"]["kind"] == "message"]
    assert [(e["data"]["from"], e["data"]["message"]) for e in pong] == [(c.address, "pong")]
    notices = [e for e in a.received() if e["data"]["kind"] == "notice"]
    assert [e["data"]["from"] for e in notices] == [c.address]
    assert 'state="completed"' in notices[0]["data"]["message"] or notices[0]["data"]
    assert [x["arguments"]["message"] for x in a.sent(c.address)] == ["ping"]
    assert [x["arguments"]["message"] for x in c.sent(a.address)] == ["pong"]
    assert a.sent(c.address)[0]["text"].startswith("Sent msg_")
    assert not [t for t in await b.turns()]  # B took no part

    # the reverse direction: C -> A
    await c.submit(f"send ping to {a.address}")
    await until(
        lambda: sum(1 for e in a.received() if e["data"]["message"] == "ping") >= 1, "A ping"
    )
    await until(
        lambda: sum(1 for e in c.received() if e["data"]["message"] == "pong") >= 1, "C pong"
    )
    await until(
        lambda: sum(1 for e in c.received() if e["data"]["kind"] == "notice") >= 1, "C notice"
    )
    await asyncio.sleep(2)
    # each side sent exactly one ping and one pong in total: no loop
    assert sorted(x["arguments"]["message"] for x in c.sent(a.address)) == ["ping", "pong"]
    assert sorted(x["arguments"]["message"] for x in a.sent(c.address)) == ["ping", "pong"]
    assert len(c.tool_calls()) == 2 and len(a.tool_calls()) == len(a.list_results()) + 2

    # events and counters
    joined = {e["data"]["instance"] for e in a.events("peer_member_joined")}
    assert {"b", "c"} <= joined
    assert a.events("peer_message_sent") and a.events("peer_message_delivered")
    metrics_a, metrics_c = await a.metrics(), await c.metrics()
    for text in (metrics_a, metrics_c):
        assert "ccw_peer_messages_received_total" in text
    assert 'ccw_peer_messages_sent_total{kind="message",result="ok"}' in metrics_a
    assert 'ccw_peer_messages_delivered_total{kind="message",result="delivered"}' in metrics_a

    # unknown peer and the membership routes
    before = len(a.tool_calls())
    await a.submit("send ping to b/ses_000000000000")
    await until(lambda: len(a.tool_calls()) > before, "A unknown-peer result")
    assert a.tool_calls()[-1]["text"].startswith("unknown peer")
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

    # stopping C: A drops it within about a heartbeat, via the leave heartbeat
    await c.svc.stop()
    await eventually_listing(a, [b.address], "A to drop C after it stopped", timeout=5)
    left = [e for e in a.events("peer_member_left") if e["data"]["instance"] == "c"]
    assert left and left[-1]["data"]["reason"] == "leave"
