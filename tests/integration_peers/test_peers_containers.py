"""Two real ccw containers: join, listing, messages both ways, and leave on container stop.

Tests run in file order and share the stack: the last one stops ccw-b.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration

DEADLINE = 60.0
HEARTBEAT = 1.0


def wait_for(probe: Callable[[], Any], what: str, timeout: float = DEADLINE) -> Any:
    end = time.monotonic() + timeout
    while True:
        value = probe()
        if value:
            return value
        if time.monotonic() > end:
            raise AssertionError(f"timed out after {timeout:g}s waiting for {what}")
        time.sleep(0.25)


@dataclass
class Node:
    name: str
    api: httpx.Client
    service: str
    sid: str = ""

    @property
    def address(self) -> str:
        return f"{self.name}/{self.sid}"

    def create_session(self) -> None:
        created = self.api.post("/v1/sessions", json={"name": f"peer-{self.name}"})
        assert created.status_code == 201, created.text
        self.sid = created.json()["session_id"]
        state = wait_for(
            lambda: (
                (s := self.api.get(f"/v1/sessions/{self.sid}").json())["state"]
                in ("ready", "failed")
                and s["state"]
            ),
            f"{self.name} session ready",
        )
        assert state == "ready"

    def submit(self, text: str) -> str:
        """POST a turn, retrying while the session is busy; returns the turn id."""
        end = time.monotonic() + DEADLINE
        while True:
            resp = self.api.post(
                f"/v1/sessions/{self.sid}/turns", json={"text": text, "lane": "idle"}
            )
            if resp.status_code != 409 or time.monotonic() > end:
                break
            time.sleep(0.25)
        assert resp.status_code == 202, f"{self.name} turn {text!r}: {resp.text}"
        return resp.json()["turn_id"]

    def tool_results(self, tool: str) -> list[str]:
        """The response text of every ccw MCP `tool` result, oldest first."""
        out: list[str] = []
        after = 0
        while True:
            page = self.api.get(
                f"/v1/sessions/{self.sid}/history", params={"after_seq": after, "limit": 500}
            ).json()
            out += [
                e["data"]["response"]
                for e in page["events"]
                if e["type"] == "tool_result" and e["data"]["tool_name"].endswith(f"__{tool}")
            ]
            if not page["has_more"]:
                return out
            after = page["next_seq"]

    def peer_turns(self) -> list[str]:
        """The prompt text of every peer-origin turn, oldest first."""
        turns = self.api.get(f"/v1/sessions/{self.sid}/turns").json()["turns"]
        return [
            self.api.get(f"/v1/sessions/{self.sid}/turns/{t['turn_id']}").json()["text"]
            for t in turns
            if t["origin"] == "peer"
        ]

    def listing(self) -> list[str]:
        """Run `list peers` and return the addresses of the newest result."""
        before = len(self.tool_results("peers"))
        self.submit("list peers")
        results = wait_for(
            lambda: (r := self.tool_results("peers")) and len(r) > before and r,
            f"{self.name} `peers` tool result",
        )
        return sorted(row["address"] for row in json.loads(results[-1]))

    def sends(self) -> list[str]:
        return [r for r in self.tool_results("send_message") if r.startswith("Sent msg_")]


@pytest.fixture(scope="module")
def a(client_a: httpx.Client) -> Node:
    return Node("a", client_a, "ccw-a")


@pytest.fixture(scope="module")
def b(client_b: httpx.Client) -> Node:
    return Node("b", client_b, "ccw-b")


def hook_log(compose: Callable[..., str], node: Node) -> list[str]:
    out = compose("exec", "-T", node.service, "cat", "/sessions/peer-hooks.log", check=False)
    return out.split()


def exchange(src: Node, dst: Node) -> None:
    """src's agent sends ping to dst; dst answers pong; src gets the idle notice; then quiet."""
    src_sends, dst_sends = len(src.sends()), len(dst.sends())
    src_turns, dst_turns = len(src.peer_turns()), len(dst.peer_turns())
    src.submit(f"send ping to {dst.address}")
    wait_for(lambda: len(dst.peer_turns()) > dst_turns, f"{dst.name} to receive the ping")
    ping = dst.peer_turns()[dst_turns]
    assert ping.startswith(f'<agent-message from="{src.address}" msg_id="msg_')
    assert "\nping\n</agent-message>" in ping
    wait_for(
        lambda: any(
            t.startswith(f'<agent-message from="{dst.address}"') and "\npong\n" in t
            for t in src.peer_turns()[src_turns:]
        ),
        f"{src.name} to receive the pong",
    )
    wait_for(
        lambda: any(t.startswith("<peer-idle-notice") for t in src.peer_turns()[src_turns:]),
        f"{src.name} to receive the idle notice",
    )
    time.sleep(3)  # a loop would show up as extra messages
    assert len(src.sends()) == src_sends + 1
    assert len(dst.sends()) == dst_sends + 1
    assert len(dst.peer_turns()) == dst_turns + 1
    notices = [t for t in src.peer_turns()[src_turns:] if t.startswith("<peer-idle-notice")]
    assert len(notices) == 1


def test_both_containers_joined(compose: Callable[..., str], a: Node, b: Node) -> None:
    for node in (a, b):
        wait_for(
            lambda n=node: "postJoin" in hook_log(compose, n),
            f"postJoin in the {node.service} hook log",
        )


def test_listing_names_the_other_session(a: Node, b: Node) -> None:
    a.create_session()
    b.create_session()
    deadline = time.monotonic() + DEADLINE
    got_a: list[str] = []
    got_b: list[str] = []
    while time.monotonic() < deadline and (got_a != [b.address] or got_b != [a.address]):
        got_a, got_b = a.listing(), b.listing()
    assert got_a == [b.address], f"A listing: {got_a}"
    assert got_b == [a.address], f"B listing: {got_b}"


def test_a_messages_b(a: Node, b: Node) -> None:
    exchange(a, b)


def test_b_messages_a(a: Node, b: Node) -> None:
    exchange(b, a)
    assert len(a.sends()) == 2
    assert len(b.sends()) == 2


def test_stopping_b_makes_it_leave(
    compose: Callable[..., str], a: Node, b: Node, tmp_path: Path
) -> None:
    compose("stop", "ccw-b")
    # the leaving heartbeat goes out while the container stops; no missed-heartbeat wait needed
    wait_for(
        lambda: a.listing() == [],
        "A's listing to drop b after ccw-b stopped",
        timeout=2 * HEARTBEAT + 10,
    )
    copy = tmp_path / "peer-hooks.log"
    compose("cp", "ccw-b:/sessions/peer-hooks.log", str(copy))  # exec needs a running container
    assert "preLeave" in copy.read_text().split()
