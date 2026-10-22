"""Peer join before any session starts, leave before any onDestroy, and `ccw serve --peer`."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from conftest import World

from claude_code_wrapper import cli
from claude_code_wrapper.service import ServiceHandle, run_service

TOKEN = "peer-token-0123456789abcdef"
AUTH = {"Authorization": "Bearer tok-abcdefgh"}


def marker(out: Path, name: str, blocking: bool = True) -> dict:
    run = f"cat >/dev/null; echo {name} >> '{out}'"
    return {"type": "command", "run": run, "blocking": blocking}


class Stub:
    """The address of another member: forwards to it and records `leaving` heartbeats."""

    def __init__(self, out: Path) -> None:
        self.out = out
        self.target = ""
        self.server: asyncio.Server | None = None
        self.port = 0

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            m = re.search(rb"content-length:\s*(\d+)", head, re.I)
            body = await reader.readexactly(int(m.group(1))) if m else b""
            if re.search(rb'"leaving"\s*:\s*true', body):
                with self.out.open("a") as f:
                    f.write("leaving-heartbeat\n")
            path = head.split(b" ", 2)[1].decode()
            sent = {"authorization", "content-type", "x-ccw-peer", "x-ccw-ts", "x-ccw-nonce"}
            headers = {}
            for raw in head.split(b"\r\n")[1:]:
                k, _, v = raw.decode().partition(":")
                if k.lower() in sent or k.lower().startswith("x-"):
                    headers[k] = v.strip()
            async with httpx.AsyncClient() as c:
                r = await c.post(self.target + path, headers=headers, content=body)
            writer.write(
                b"HTTP/1.1 %d x\r\ncontent-type: application/json\r\ncontent-length: %d\r\n"
                b"connection: close\r\n\r\n%s" % (r.status_code, len(r.content), r.content)
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def close(self) -> None:
        assert self.server is not None
        self.server.close()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.server.wait_closed(), 2)


@pytest.fixture
async def stub(tmp_path: Path) -> AsyncIterator[Stub]:
    s = Stub(tmp_path / "marks")
    await s.start()
    yield s
    await s.close()


def make_cfg(
    world: World,
    seeds: list[str],
    hooks: dict,
    enabled: bool = True,
    name: str = "me",
    advertise_url: str = "http://127.0.0.1:1",
    **sections,
):
    cfg = world.cfg(
        hooks=hooks,
        peers={
            "enabled": enabled,
            "name": name,
            "advertise_url": advertise_url,
            "token": TOKEN,
            "static": seeds,
            "heartbeat_seconds": 30,
            "join_timeout_seconds": 1,
        },
        **sections,
    )
    server = cfg.server.model_copy(
        update={"host": "127.0.0.1", "port": 0, "internal_port": 0, "auth_token": "tok-abcdefgh"}
    )
    return cfg.model_copy(update={"server": server})


def all_hooks(out: Path) -> dict:
    return {
        n: [marker(out, n)]
        for n in ("preJoin", "postJoin", "preLeave", "postLeave", "onCreate", "onDestroy")
    }


def lines(out: Path) -> list[str]:
    return out.read_text().split() if out.exists() else []


async def boot(cfg):
    ready, stop, handle = asyncio.Event(), asyncio.Event(), ServiceHandle()
    task = asyncio.create_task(run_service(cfg, ready=ready, stop=stop, handle=handle))
    waiter = asyncio.create_task(ready.wait())
    await asyncio.wait({task, waiter}, timeout=20, return_when=asyncio.FIRST_COMPLETED)
    waiter.cancel()
    if task.done():
        task.result()
    assert ready.is_set()
    return task, stop, handle, f"http://127.0.0.1:{handle.public_port}"


@pytest.fixture
async def member(world: World, stub: Stub) -> AsyncIterator[Stub]:
    """A second real service (peers on) reachable through `stub`."""
    cfg = make_cfg(world, [], {}, name="b", advertise_url=stub.url)
    task, stop, handle, _pub = await boot(cfg)
    stub.target = f"http://127.0.0.1:{handle.public_port}"
    yield stub
    stop.set()
    await asyncio.wait_for(task, 20)


async def test_join_precedes_first_session_and_leave_precedes_destroy(
    world: World, member: Stub
) -> None:
    stub = member
    out = stub.out
    task, stop, _handle, pub = await boot(make_cfg(world, [stub.url], all_hooks(out)))
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{pub}/v1/sessions", json={}, headers=AUTH)
        assert r.status_code == 201
    assert lines(out)[:3] == ["preJoin", "postJoin", "onCreate"]
    stop.set()
    await asyncio.wait_for(task, 20)
    assert lines(out) == [
        "preJoin",
        "postJoin",
        "onCreate",
        "preLeave",
        "leaving-heartbeat",
        "postLeave",
        "onDestroy",
    ]


async def test_unreachable_seed_still_becomes_ready(world: World, tmp_path: Path) -> None:
    out = tmp_path / "marks"
    start = time.monotonic()
    task, stop, _h, _pub = await boot(make_cfg(world, ["http://127.0.0.1:1"], all_hooks(out)))
    assert time.monotonic() - start < 1 + 10
    stop.set()
    await asyncio.wait_for(task, 20)
    assert "postJoin" in lines(out)


async def test_exit_when_last_destroyed_leaves_before_on_destroy(
    world: World, member: Stub
) -> None:
    stub = member
    out = stub.out
    cfg = make_cfg(world, [stub.url], all_hooks(out), sessions={"exit_when_last_destroyed": True})
    task, _stop, handle, pub = await boot(cfg)
    async with httpx.AsyncClient(timeout=20) as c:
        sid = (await c.post(f"{pub}/v1/sessions", json={}, headers=AUTH)).json()["session_id"]
        assert (await c.delete(f"{pub}/v1/sessions/{sid}", headers=AUTH)).status_code == 200
    await asyncio.wait_for(task, 20)
    assert handle.exit_code == 0
    assert lines(out)[2:] == [
        "onCreate",
        "preLeave",
        "leaving-heartbeat",
        "postLeave",
        "onDestroy",
    ]


async def test_destroy_with_other_session_keeps_membership(world: World, stub: Stub) -> None:
    out = stub.out
    cfg = make_cfg(world, [stub.url], all_hooks(out), sessions={"exit_when_last_destroyed": True})
    task, stop, _h, pub = await boot(cfg)
    async with httpx.AsyncClient(timeout=20) as c:
        a = (await c.post(f"{pub}/v1/sessions", json={}, headers=AUTH)).json()["session_id"]
        await c.post(f"{pub}/v1/sessions", json={}, headers=AUTH)
        await c.delete(f"{pub}/v1/sessions/{a}", headers=AUTH)
    assert "preLeave" not in lines(out)
    stop.set()
    await asyncio.wait_for(task, 20)


async def test_peers_disabled_no_endpoint_no_hooks(world: World, tmp_path: Path) -> None:
    out = tmp_path / "marks"
    task, stop, _h, pub = await boot(make_cfg(world, [], all_hooks(out), enabled=False))
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{pub}/v1/peers/heartbeat", json={}, headers=AUTH)
        assert r.status_code == 404
    stop.set()
    await asyncio.wait_for(task, 20)
    assert not set(lines(out)) & {"preJoin", "postJoin", "preLeave", "postLeave"}


# ---------------------------------------------------------------------- cli


def peers_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "c.yaml"
    p.write_text(
        "server: {auth_token: tok-abcdefgh}\n"
        f"peers: {{enabled: true, token: {TOKEN}, static: ['http://a:1']}}\n"
        f"paths: {{workdir_root: {tmp_path}/wd, sessions_root: {tmp_path}/ss}}\n"
    )
    return p


def test_cli_peer_flag_extends_static(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_claude_bin: Path
) -> None:
    monkeypatch.setenv("PATH", f"{fake_claude_bin.parent}{os.pathsep}{os.environ['PATH']}")
    seen = []

    async def fake(cfg, **kw):
        seen.append(cfg)

    monkeypatch.setattr(cli, "run_service", fake)
    args = ["serve", "--config", str(peers_yaml(tmp_path))]
    assert cli.main([*args, "--peer", "http://b:2", "--peer", "http://c:3"]) == 0
    assert list(seen[0].peers.static) == ["http://a:1", "http://b:2", "http://c:3"]


def test_cli_bad_peer_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_claude_bin: Path, capsys
) -> None:
    monkeypatch.setenv("PATH", f"{fake_claude_bin.parent}{os.pathsep}{os.environ['PATH']}")
    assert cli.main(["serve", "--config", str(peers_yaml(tmp_path)), "--peer", "nope"]) == 2
    assert capsys.readouterr().err


def test_cli_peers_startup_error_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_claude_bin: Path, capsys
) -> None:
    from claude_code_wrapper.peer_client import PeerError

    monkeypatch.setenv("PATH", f"{fake_claude_bin.parent}{os.pathsep}{os.environ['PATH']}")

    async def fake(cfg, **kw):
        raise PeerError("x", "boom")

    monkeypatch.setattr(cli, "run_service", fake)
    assert cli.main(["serve", "--config", str(peers_yaml(tmp_path))]) == 1
    assert "ccw: peers: boom" in capsys.readouterr().err
