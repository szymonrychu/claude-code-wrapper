"""The terminal registry (src/claude_code_wrapper/terminals.py): interactive login shells in a
session workdir, shared by several subscribers, reaped when idle and closed with their session.

Every test runs real children under a PTY: `/bin/sh` for the registry-level tests, the default
login shell (bash when present, else sh) through a real `SessionManager` for the manager ones.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import World, wait_until
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_api_terminals import AUTH, Host, StubManager

from claude_code_wrapper import service as service_mod
from claude_code_wrapper import terminals as terminals_mod
from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.claude_env import ClaudeEnv
from claude_code_wrapper.config import Config, LimitsConfig
from claude_code_wrapper.manager import CreateSessionRequest, SessionManager, SessionNotFound
from claude_code_wrapper.pty_proc import PtyProcess
from claude_code_wrapper.redact import Redactor
from claude_code_wrapper.terminals import (
    SCRUBBED_ENV,
    Attachment,
    InvalidSize,
    SessionNotActive,
    ShellTarget,
    SubscriberOverflow,
    TerminalExit,
    TerminalLimit,
    TerminalNotFound,
    TerminalRegistry,
    TooManyClients,
    login_shell_argv,
    shell_env,
)

SH = ("/bin/sh",)
TERMINAL_ID = re.compile(r"^term_[0-9a-f]{12}$")
UNKNOWN_SESSION = "ses_000000000000"
SECRET_NAMES = sorted(s.env for s in ClaudeEnv.catalogue() if s.secret)
CCW_TOKENS = ("CCW_AUTH_TOKEN", "CCW_PEER_TOKEN", "CCW_HOOK_TOKEN")


# ------------------------------------------------------------------ helpers


class FakeHost:
    """Stands in for the SessionManager: known sessions, the active ones and their workdirs."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.workdirs: dict[str, Path] = {}
        self.active: set[str] = set()

    def add(self, session_id: str, active: bool = True) -> Path:
        workdir = self.tmp / f"wd-{session_id}"
        workdir.mkdir(exist_ok=True)
        self.workdirs[session_id] = workdir
        if active:
            self.active.add(session_id)
        return workdir

    def resolve(self, session_id: str) -> ShellTarget:
        if session_id not in self.workdirs:
            raise SessionNotFound(session_id)
        if session_id not in self.active:
            raise SessionNotActive(session_id)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TERM": "xterm-256color"}
        return ShellTarget(workdir=str(self.workdirs[session_id]), env=env)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


SID = "ses_aaaaaaaaaaaa"


class Maker:
    """Builds registries over a FakeHost (session `SID` active) and shuts them all down."""

    def __init__(self, tmp_path: Path) -> None:
        self.host = FakeHost(tmp_path)
        self.host.add(SID)
        self.built: list[TerminalRegistry] = []

    def __call__(self, **kw: Any) -> TerminalRegistry:
        limits = LimitsConfig(**kw.pop("limits", {}))
        kw.setdefault("argv", SH)
        reg = TerminalRegistry(limits, self.host.resolve, **kw)
        self.built.append(reg)
        return reg


@pytest.fixture
async def make(tmp_path: Path):
    maker = Maker(tmp_path)
    yield maker
    for reg in maker.built:
        await reg.shutdown(1.0)


async def read_until(
    att: Attachment, pred: Callable[[bytes], bool], timeout: float = 10.0
) -> tuple[bytes, TerminalExit | None]:
    """Live bytes from `att` until `pred(buf)` holds or the exit marker arrives."""
    buf = b""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred(buf):
        remaining = end - loop.time()
        if remaining <= 0:
            raise AssertionError(f"timed out; got {buf[-400:]!r}")
        item = await asyncio.wait_for(att.next(), remaining)
        if isinstance(item, TerminalExit):
            return buf, item
        buf += item
    return buf, None


async def run(att: Attachment, command: str, marker: bytes, timeout: float = 10.0) -> bytes:
    """Type `command` and read until `marker` (an arithmetic result the echo cannot contain)."""
    await att.write(command.encode() + b"\n")
    buf, ended = await read_until(att, lambda b: marker in b, timeout)
    assert ended is None, f"shell exited early: {buf[-400:]!r}"
    return buf


async def drain_quiet(att: Attachment, quiet: float = 0.3) -> bytes:
    """Whatever arrives until `quiet` seconds pass without output."""
    buf = b""
    while True:
        try:
            item = await asyncio.wait_for(att.next(), quiet)
        except TimeoutError:
            return buf
        assert not isinstance(item, TerminalExit)
        buf += item


def proc_state(pid: int) -> str | None:
    """The /proc state letter of `pid`, None when it is gone."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return stat.rpartition(")")[2].split()[0]


def live(pid: int) -> bool:
    """Running (a zombie is dead: PID 1 of this container may never reap it)."""
    return proc_state(pid) not in (None, "Z")


def session_members(sid: int) -> list[int]:
    out: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
        except OSError:
            continue
        fields = stat.rpartition(")")[2].split()
        if int(fields[3]) == sid and fields[0] != "Z":
            out.append(int(entry))
    return out


async def start_background_job(att: Attachment) -> int:
    """Start a `nohup`'d sleep in the shell and return its pid."""
    out = await run(att, 'nohup sleep 300 >/dev/null 2>&1 & echo "BG<$!>"', b"BG<")
    out += (await read_until(att, lambda b: re.search(rb"BG<(\d+)>", out + b) is not None))[0]
    match = re.search(rb"BG<(\d+)>", out)
    assert match, out
    pid = int(match.group(1))
    assert live(pid)
    return pid


# ------------------------------------------------------------------ config


def test_limits_terminal_keys() -> None:
    limits = LimitsConfig()
    assert limits.terminals_max == 8
    assert limits.terminal_scrollback_bytes == 262144
    assert limits.terminal_idle_reap_seconds == 900
    assert limits.terminal_max_frame_bytes == 65536
    assert limits.terminal_max_subscribers == 8
    assert LimitsConfig(terminals_max=0).terminals_max == 0
    for bad in (
        {"terminals_max": -1},
        {"terminal_scrollback_bytes": 0},
        {"terminal_idle_reap_seconds": 0},
        {"terminal_max_frame_bytes": 0},
        {"terminal_max_subscribers": 0},
    ):
        with pytest.raises(ValidationError):
            LimitsConfig(**bad)


# ------------------------------------------------------------------ shell


def test_default_shell_is_login_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terminals_mod.os.path, "exists", lambda p: p == "/bin/bash")
    assert login_shell_argv() == ("/bin/bash", "-l")
    monkeypatch.setattr(terminals_mod.os.path, "exists", lambda p: False)
    assert login_shell_argv() == ("/bin/sh", "-l")


async def test_create_runs_shell_in_workdir(make, tmp_path: Path) -> None:
    reg = make()
    real = make.host.workdirs[SID]
    link = tmp_path / "link"
    link.symlink_to(real)
    make.host.workdirs[SID] = link  # the cwd is the resolved (real) workdir
    info = await reg.create(SID, rows=30, cols=100)
    assert TERMINAL_ID.match(info.terminal_id)
    assert info.session_id == SID
    assert (info.rows, info.cols, info.alive, info.exit_code, info.attached) == (
        30,
        100,
        True,
        None,
        0,
    )
    assert info.created_at.endswith("Z")
    assert [t.terminal_id for t in reg.list(SID)] == [info.terminal_id]
    assert reg.get(SID, info.terminal_id).terminal_id == info.terminal_id
    att = reg.attach(SID, info.terminal_id)
    assert reg.get(SID, info.terminal_id).attached == 1
    out = await run(att, "pwd; echo DONE$((1+1))", b"DONE2")
    assert str(real.resolve()).encode() in out
    # Input echoes back through the subscriber.
    out = await run(att, "echo typed-$((40+2))-back", b"typed-42-back")
    assert b"echo typed-$((40+2))-back" in out
    att.resize(40, 120)
    out = await run(att, "stty size; echo SZ$((3+3))", b"SZ6")
    assert b"40 120" in out
    with pytest.raises(InvalidSize):
        att.resize(0, 80)
    att.detach()
    att.detach()  # idempotent
    assert reg.get(SID, info.terminal_id).attached == 0
    with pytest.raises(TerminalNotFound):
        reg.get(SID, "term_000000000000")
    with pytest.raises(TerminalNotFound):
        reg.get("ses_bbbbbbbbbbbb", info.terminal_id)  # another session's id space


async def test_invalid_size_rejected(make) -> None:
    reg = make(limits={"terminals_max": 1})
    for rows, cols in ((0, 80), (1001, 80), (24, 0), (24, 1001), (-5, 80)):
        with pytest.raises(InvalidSize):
            await reg.create(SID, rows=rows, cols=cols)
    assert reg.list(SID) == []
    # No slot was consumed by the refusals; the edges are accepted.
    info = await reg.create(SID, rows=1000, cols=1)
    assert (info.rows, info.cols) == (1000, 1)


# ------------------------------------------------------------------ cap


async def test_cap_per_process(make) -> None:
    make.host.add("ses_bbbbbbbbbbbb")
    reg = make()
    for i in range(8):
        await reg.create(SID if i % 2 else "ses_bbbbbbbbbbbb")
    with pytest.raises(TerminalLimit):
        await reg.create(SID)
    with pytest.raises(TerminalLimit):
        await reg.create("ses_bbbbbbbbbbbb")
    assert len(reg.list(SID)) + len(reg.list("ses_bbbbbbbbbbbb")) == 8
    disabled = make(limits={"terminals_max": 0})
    with pytest.raises(TerminalLimit):
        await disabled.create(SID)


async def test_concurrent_creates_never_exceed_cap(make, monkeypatch: pytest.MonkeyPatch) -> None:
    reg = make(limits={"terminals_max": 3})
    original = PtyProcess.start
    seen: list[int] = []
    fail_next = [True]

    async def slow_start(self: PtyProcess, *a: Any, **kw: Any) -> None:
        seen.append(reg.count())
        await asyncio.sleep(0.05)  # every create is in flight at once
        if fail_next[0]:
            fail_next[0] = False
            raise OSError("spawn failed")
        await original(self, *a, **kw)

    monkeypatch.setattr(PtyProcess, "start", slow_start)
    results = await asyncio.gather(*(reg.create(SID) for _ in range(10)), return_exceptions=True)
    assert max(seen) <= 3
    assert reg.count() <= 3
    failed = [r for r in results if isinstance(r, OSError)]
    limited = [r for r in results if isinstance(r, TerminalLimit)]
    created = [r for r in results if not isinstance(r, BaseException)]
    assert len(failed) == 1 and len(created) == 2 and len(limited) == 7
    # The failed start released its slot: one more fits, then the cap holds again.
    assert reg.count() == 2
    await reg.create(SID)
    with pytest.raises(TerminalLimit):
        await reg.create(SID)
    assert reg.count() == 3


async def test_cap_counts_exited_and_reaps_exited(make) -> None:
    clock = Clock()
    reg = make(limits={"terminals_max": 2}, clock=clock, reap_interval=3600)
    t1 = await reg.create(SID)
    t2 = await reg.create(SID)
    a1 = reg.attach(SID, t1.terminal_id)
    await a1.write(b"exit 4\n")
    _, ended = await read_until(a1, lambda b: False)
    assert ended == TerminalExit(4)
    a1.detach()
    a2 = reg.attach(SID, t2.terminal_id)
    await a2.write(b"exit 6\n")
    _, ended = await read_until(a2, lambda b: False)
    assert ended == TerminalExit(6)
    # Exited terminals still count against the cap.
    with pytest.raises(TerminalLimit):
        await reg.create(SID)
    # Next tick: the exited one without a client goes (no idle delay), the attached one stays.
    reaped = await reg.reap()
    assert reaped == [t1.terminal_id]
    assert [t.terminal_id for t in reg.list(SID)] == [t2.terminal_id]
    await reg.create(SID)
    a2.detach()
    assert await reg.reap() == [t2.terminal_id]


# ------------------------------------------------------------------ attach


async def test_attach_replays_then_streams(make) -> None:
    reg = make()
    info = await reg.create(SID)
    first = reg.attach(SID, info.terminal_id)
    total = first.snapshot
    total += await run(first, "echo A$((1))", b"A1")
    # A second subscriber joins in the middle of a burst of output.
    burst = (
        "i=0; while [ $i -lt 40 ]; do seq -f L$i-%g 1 50; i=$((i+1)); sleep 0.02; done;"
        " echo END$((4+5))"
    )
    await first.write(burst.encode() + b"\n")
    a, _ = await read_until(first, lambda b: b"L5-50" in b)
    second = reg.attach(SID, info.terminal_id)
    assert b"L5-50" in second.snapshot and b"END9" not in second.snapshot  # mid-burst
    a += (await read_until(first, lambda b: b"END9" in a + b))[0]
    b, _ = await read_until(second, lambda b: b"END9" in b)
    total += a + await drain_quiet(first)
    joined = second.snapshot + b + await drain_quiet(second)
    term_ring = reg.ring(SID, info.terminal_id)
    assert len(total) < term_ring.capacity  # nothing was trimmed, so all three must agree
    assert joined == total  # no gap and no duplicate between replay and live output
    assert term_ring.raw() == total
    assert b"L39-50" in joined


async def test_slow_subscriber_dropped_not_blocking(make) -> None:
    reg = make(queue_bytes=2048)
    info = await reg.create(SID)
    slow = reg.attach(SID, info.terminal_id)  # never reads
    fast = reg.attach(SID, info.terminal_id)
    burst = "i=0; while [ $i -lt 2000 ]; do echo line$i; i=$((i+1)); done; echo END$((1+6))"
    out = await run(fast, burst, b"END7")
    assert b"line1999" in out
    assert slow.overflowed
    with pytest.raises(SubscriberOverflow):
        await asyncio.wait_for(slow.next(), 1)
    assert reg.get(SID, info.terminal_id).attached == 1  # only the fast one is left
    out = await run(fast, "echo again$((2*4))", b"again8")
    slow.detach()  # harmless after the drop
    assert reg.get(SID, info.terminal_id).attached == 1


async def test_exit_reported(make) -> None:
    reg = make()
    info = await reg.create(SID)
    one = reg.attach(SID, info.terminal_id)
    two = reg.attach(SID, info.terminal_id)
    await one.write(b"exit 3\n")
    for att in (one, two):
        _, ended = await read_until(att, lambda b: False)
        assert ended == TerminalExit(3)
    after = reg.get(SID, info.terminal_id)
    assert (after.alive, after.exit_code) == (False, 3)
    assert [t.terminal_id for t in reg.list(SID)] == [info.terminal_id]  # still listed
    with pytest.raises(terminals_mod.PtyClosed):
        await one.write(b"echo\n")


async def test_attach_exited_replays_then_exit(make) -> None:
    reg = make()
    info = await reg.create(SID)
    att = reg.attach(SID, info.terminal_id)
    await att.write(b"echo X$((2+2)); exit 5\n")
    out, ended = await read_until(att, lambda b: False)
    assert ended == TerminalExit(5) and b"X4" in out
    att.detach()
    late = reg.attach(SID, info.terminal_id)
    assert b"X4" in late.snapshot
    assert await asyncio.wait_for(late.next(), 1) == TerminalExit(5)


# ------------------------------------------------------------------ reaper


async def test_reaper_respects_attachment(make) -> None:
    reg = make(limits={"terminal_idle_reap_seconds": 0.3}, reap_interval=0.05)
    kept = await reg.create(SID)
    idle = await reg.create(SID)
    att = reg.attach(SID, kept.terminal_id)
    ids = lambda: {t.terminal_id for t in reg.list(SID)}  # noqa: E731
    await wait_until(lambda: idle.terminal_id not in ids(), 5, "idle terminal reaped")
    await asyncio.sleep(0.8)  # well past the idle delay: attached, so never reaped
    assert kept.terminal_id in ids()
    assert reg.get(SID, kept.terminal_id).alive
    att.detach()
    await wait_until(lambda: kept.terminal_id not in ids(), 5, "detached terminal reaped")


async def test_idle_clock_creation_and_detach(make) -> None:
    clock = Clock()
    reg = make(limits={"terminal_idle_reap_seconds": 100}, clock=clock, reap_interval=3600)
    never = await reg.create(SID)  # never attached: the clock runs from creation
    clock.now += 99
    assert await reg.reap() == []
    clock.now += 1
    assert await reg.reap() == [never.terminal_id]
    used = await reg.create(SID)
    clock.now += 50
    att = reg.attach(SID, used.terminal_id)
    clock.now += 500  # attached: no clock at all
    assert await reg.reap() == []
    att.detach()  # the clock restarts at the last detach
    clock.now += 99
    assert await reg.reap() == []
    again = reg.attach(SID, used.terminal_id)
    other = reg.attach(SID, used.terminal_id)
    again.detach()  # one client left: still attached
    clock.now += 500
    assert await reg.reap() == []
    other.detach()
    clock.now += 99
    assert await reg.reap() == []
    clock.now += 1
    assert await reg.reap() == [used.terminal_id]
    assert reg.list(SID) == []


async def test_attach_during_reap_never_hangs(make) -> None:
    clock = Clock()
    reg = make(limits={"terminal_idle_reap_seconds": 10}, clock=clock, reap_interval=3600)
    # Attached before the close runs: the subscriber gets the exit marker.
    info = await reg.create(SID)
    closing = asyncio.ensure_future(reg.close(SID, info.terminal_id))
    att = reg.attach(SID, info.terminal_id)
    _, ended = await read_until(att, lambda b: False, timeout=5)
    assert isinstance(ended, TerminalExit)
    assert await closing is True
    # The close has started: the terminal is already unregistered.
    info = await reg.create(SID)
    closing = asyncio.ensure_future(reg.close(SID, info.terminal_id))
    await asyncio.sleep(0)
    with pytest.raises(TerminalNotFound):
        reg.attach(SID, info.terminal_id)
    await closing
    assert await reg.close(SID, info.terminal_id) is False  # idempotent
    # The same against the reaper, at random interleavings.
    rng = random.Random(7)
    for _ in range(12):
        info = await reg.create(SID)
        clock.now += 11
        reaping = asyncio.ensure_future(reg.reap())
        for _ in range(rng.randrange(3)):
            await asyncio.sleep(0)
        try:
            att = reg.attach(SID, info.terminal_id)
        except TerminalNotFound:
            pass
        else:  # attached in time: either never reaped, or reaped with the exit marker
            if info.terminal_id in {t.terminal_id for t in reg.list(SID)}:
                await reg.close(SID, info.terminal_id)
            _, ended = await read_until(att, lambda b: False, timeout=5)
            assert isinstance(ended, TerminalExit)
        await asyncio.wait_for(reaping, 5)


async def test_close_kills_whole_shell_session(make) -> None:
    reg = make()
    info = await reg.create(SID)
    att = reg.attach(SID, info.terminal_id)
    bg = await start_background_job(att)
    shell = reg.pid(SID, info.terminal_id)
    assert bg in session_members(shell)
    assert await reg.close(SID, info.terminal_id) is True
    assert not live(bg)
    assert session_members(shell) == []
    with pytest.raises(TerminalNotFound):
        reg.get(SID, info.terminal_id)


async def test_close_skips_reused_pid(make) -> None:
    """An exited shell whose pid now belongs to another session leader is closed without
    signalling that session: the recorded start time no longer matches."""
    reg = make()
    info = await reg.create(SID)
    shell = reg.pid(SID, info.terminal_id)
    os.kill(shell, 9)
    await wait_until(lambda: reg.get(SID, info.terminal_id).alive is False, timeout=10)
    victim = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        term = reg._terminals[info.terminal_id]
        term.pty.pid = victim.pid  # the pid was reused by an unrelated session leader
        assert victim.pid in session_members(victim.pid)
        assert await reg.close(SID, info.terminal_id) is True
        assert victim.poll() is None, "the unrelated session was signalled"
        assert live(victim.pid)
    finally:
        victim.kill()
        victim.wait()


async def test_close_skips_vanished_pid(make) -> None:
    reg = make()
    info = await reg.create(SID)
    term = reg._terminals[info.terminal_id]
    assert term.shell_start is not None
    os.kill(term.pty.pid, 9)
    await wait_until(lambda: reg.get(SID, info.terminal_id).alive is False, timeout=10)
    assert terminals_mod._start_time(2**22 + 12345) is None
    assert await reg.close(SID, info.terminal_id) is True


def test_shell_env_drops_secret_named_keys() -> None:
    env = shell_env(
        {
            "PATH": "/usr/bin",
            "MY_SERVICE_TOKEN": "t",
            "db_password": "p",
            "SOME_API_KEY": "k",
            "MY_PLAIN": "ok",
        }
    )
    assert env == {"PATH": "/usr/bin", "MY_PLAIN": "ok", "TERM": "xterm-256color"}


def test_uvicorn_ws_max_size_matches_frame_cap() -> None:
    cfg = Config.model_validate({"limits": {"terminal_max_frame_bytes": 4321}})
    server = service_mod._server(object(), cfg)
    assert server.config.ws_max_size == 4321


async def test_attach_beyond_subscriber_cap_refused(make) -> None:
    reg = make(limits={"terminal_max_subscribers": 2})
    info = await reg.create(SID)
    a = reg.attach(SID, info.terminal_id)
    b = reg.attach(SID, info.terminal_id)
    with pytest.raises(TooManyClients):
        reg.attach(SID, info.terminal_id)
    a.detach()
    c = reg.attach(SID, info.terminal_id)  # a freed slot is reusable
    b.detach()
    c.detach()


def test_attach_beyond_subscriber_cap_closed_1013(tmp_path: Path) -> None:
    host = Host(tmp_path)
    limits = {"terminal_max_subscribers": 1}
    registry = TerminalRegistry(LimitsConfig(**limits), host.resolve, argv=SH)
    cfg = Config.model_validate(
        {"server": {"auth_token": "tok-terminals-0123456789"}, "limits": limits}
    )
    app = create_app(StubManager(host, registry), cfg)
    with TestClient(app) as client:
        created = client.post(f"/v1/sessions/{SID}/terminals", json={}, headers=AUTH)
        url = f"/v1/sessions/{SID}/terminals/{created.json()['terminal_id']}/attach"
        with (
            client.websocket_connect(url, headers=AUTH),
            client.websocket_connect(url, headers=AUTH) as second,
        ):
            msg = second.receive()
            assert msg["type"] == "websocket.close"
            assert msg["code"] == 1013
            assert msg["reason"] == "too many clients"
        client.portal.call(registry.shutdown, 1.0)


# ------------------------------------------------------------------ manager


def scrub_world_manager(world: World, service_env: dict[str, str], **sections: Any):
    cfg = world.cfg(**sections)
    m = SessionManager(cfg, internal_url=world.url, service_env=service_env, redactor=Redactor())
    world.managers.append(m)
    return m


def base_service_env() -> dict[str, str]:
    keep = ("PATH", "HOME", "LANG", "USER", "TMPDIR")
    return {k: os.environ[k] for k in keep if k in os.environ}


async def test_env_scrubbed(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    names = [*SECRET_NAMES, *CCW_TOKENS]
    service_values = {name: f"svcsentinel{i:03d}q" for i, name in enumerate(names)}
    claude_values = {n: f"cenvsentinel{i:03d}q" for i, n in enumerate(SECRET_NAMES)}
    service_env = {**base_service_env(), **service_values, "MY_PASSTHROUGH": "visible-pass"}
    m = scrub_world_manager(
        world,
        service_env,
        claude={
            "env": {name.lower(): value for name, value in claude_values.items()},
            "passthrough_env": [*SECRET_NAMES, "MY_PASSTHROUGH"],
        },
    )
    snap = await m.create(CreateSessionRequest())
    sid = snap["session_id"]
    original = m._child_env
    ccw_in_child = {"CCW_AUTH_TOKEN": "childsentinelauth", "CCW_PEER_TOKEN": "childsentinelpeer"}

    def leaky_child_env(ctx: Any) -> dict[str, str]:
        env = original(ctx)
        for name in SECRET_NAMES:  # every secret name reaches the scrub, whatever its source
            env.setdefault(name, service_values[name])
        return {**env, **ccw_in_child}

    monkeypatch.setattr(m, "_child_env", leaky_child_env)
    child = leaky_child_env(m._active[sid])
    for name in names:
        assert name in child, name  # each scrubbed name is really offered to the shell
    hook_token = m._active[sid].hook_token
    info = await m.terminals.create(sid)  # the default login shell
    att = m.terminals.attach(sid, info.terminal_id)
    out = (att.snapshot + await run(att, "env; echo ENVEND$((2+3))", b"ENVEND5")).decode(
        "utf-8", "replace"
    )
    for name in names:
        assert not re.search(rf"(^|[\r\n]){name}=", out), name
    values = [*service_values.values(), *claude_values.values(), *ccw_in_child.values()]
    for value in [*values, hook_token]:
        assert value not in out, value
    assert re.search(r"(^|[\r\n])TERM=xterm-256color\r?\n", out)
    assert re.search(r"(^|[\r\n])MY_PASSTHROUGH=visible-pass\r?\n", out)


async def test_env_matches_claude_process_env(world: World) -> None:
    env_bin = shutil.which("env")
    assert env_bin
    service_env = {
        **base_service_env(),
        "ANTHROPIC_API_KEY": "svc-api-key-sentinel",
        "CCW_AUTH_TOKEN": "svc-auth-sentinel",
        "CCW_PEER_TOKEN": "svc-peer-sentinel",
        "PASSED_THROUGH": "yes-passed",
        "SERVICE_ONLY": "not-passed",
    }
    m = scrub_world_manager(
        world,
        service_env,
        claude={
            "env": {"claude_code_oauth_token": "oauth-sentinel", "extra": {"SPEC_V": "spec"}},
            "passthrough_env": ["PASSED_THROUGH", "ANTHROPIC_API_KEY"],
        },
    )
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    m.terminals.shell_argv = (env_bin, "-0")
    info = await m.terminals.create(sid)
    att = m.terminals.attach(sid, info.terminal_id)
    out, ended = await read_until(att, lambda b: False)
    assert ended == TerminalExit(0)
    raw = (att.snapshot + out).replace(b"\r\n", b"\n")
    got = dict(item.decode().split("=", 1) for item in raw.split(b"\0") if b"=" in item)
    claude = m._child_env(m._active[sid])
    expected = {k: v for k, v in claude.items() if k not in SCRUBBED_ENV}
    assert got == expected
    assert got["TERM"] == "xterm-256color"
    assert got["PASSED_THROUGH"] == "yes-passed" and got["SPEC_V"] == "spec"
    assert "CCW_SESSION_ID" in got  # the non-secret plumbing of the claude env stays
    for gone in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", *CCW_TOKENS, "SERVICE_ONLY"):
        assert gone not in got, gone
    assert got != m.service_env


async def test_session_not_active_and_closed_on_stop(world: World) -> None:
    m = world.manager(world.cfg())
    m.terminals.shell_argv = SH
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    info = await m.terminals.create(sid)
    att = m.terminals.attach(sid, info.terminal_id)
    shell = m.terminals.pid(sid, info.terminal_id)
    await run(att, "echo up$((1+1))", b"up2")
    await m.destroy(sid)  # stop: the files stay, the session is known but not active
    assert m.get(sid)["state"] == "stopped"
    assert m.terminals.list(sid) == []
    _, ended = await read_until(att, lambda b: False, timeout=5)
    assert isinstance(ended, TerminalExit)
    assert not live(shell)
    with pytest.raises(SessionNotActive):
        await m.terminals.create(sid)
    with pytest.raises(SessionNotActive):
        m.terminals.attach(sid, info.terminal_id)
    with pytest.raises(SessionNotFound):
        await m.terminals.create(UNKNOWN_SESSION)
    with pytest.raises(SessionNotFound):
        m.terminals.attach(UNKNOWN_SESSION, info.terminal_id)


async def test_closed_on_destroy_and_shutdown(world: World, make) -> None:
    # close: the registry alone.
    reg = make()
    info = await reg.create(SID)
    bg = await start_background_job(reg.attach(SID, info.terminal_id))
    shell = reg.pid(SID, info.terminal_id)
    await reg.close(SID, info.terminal_id)
    assert not live(bg) and session_members(shell) == []

    # session destroy: every shell of the session, with its jobs.
    m = world.manager(world.cfg(sessions={"max_sessions": 2}))
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    other = (await m.create(CreateSessionRequest()))["session_id"]
    shells, jobs = [], []
    for session_id in (sid, sid, other):
        info = await m.terminals.create(session_id)
        jobs.append(await start_background_job(m.terminals.attach(session_id, info.terminal_id)))
        shells.append(m.terminals.pid(session_id, info.terminal_id))
    await m.destroy(sid)
    assert m.terminals.list(sid) == []
    for pid, shell in zip(jobs[:2], shells[:2], strict=True):
        assert not live(pid) and session_members(shell) == []
    assert live(jobs[2]) and len(m.terminals.list(other)) == 1  # another session's shell stays

    # manager shutdown: everything left, and the reaper stops.
    assert m.terminals.reaper_running
    await m.shutdown()
    assert not live(jobs[2]) and session_members(shells[2]) == []
    assert m.terminals.count() == 0
    assert not m.terminals.reaper_running
    with pytest.raises(SessionNotActive):
        await m.terminals.create(other)


def test_manager_holds_registry_with_configured_limits(tmp_path: Path) -> None:
    cfg = Config.model_validate(
        {
            "paths": {
                "workdir_root": str(tmp_path / "w"),
                "sessions_root": str(tmp_path / "s"),
            },
            "limits": {"terminals_max": 3, "terminal_scrollback_bytes": 4096},
        }
    )
    m = SessionManager(cfg, internal_url="", service_env={}, redactor=Redactor())
    assert isinstance(m.terminals, TerminalRegistry)
    assert m.terminals.limits.terminals_max == 3
    assert m.terminals.limits.terminal_scrollback_bytes == 4096
    assert {*SECRET_NAMES, *CCW_TOKENS} <= SCRUBBED_ENV
    assert "ANTHROPIC_API_KEY" in SCRUBBED_ENV and "CLAUDE_CODE_OAUTH_TOKEN" in SCRUBBED_ENV
