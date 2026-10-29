"""Command request API and the idle-path runner (design D3 request, D4 steps 1, 3 and 8, D7)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import World, jsonl
from test_metrics import sample
from test_router import Harness, StubSession

from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.commands import CommandRejected
from claude_code_wrapper.manager import CreateSessionRequest
from claude_code_wrapper.metrics import Metrics
from claude_code_wrapper.pty_proc import paste_bytes
from claude_code_wrapper.session import TurnRejected

ALL = ["compact", "reload-plugins", "reload-skills", "mcp", "context"]


def commands_cfg(**kw: Any) -> dict[str, Any]:
    return {
        "client": ALL,
        "agent": ["compact", "reload-plugins"],
        "quiet_seconds": 0.2,
        "timeout_seconds": {"compact": 5, "default": 5},
        **kw,
    }


async def start(
    world: World,
    scenario: dict | None = None,
    hooks: dict | None = None,
    **commands: Any,
) -> tuple[Any, str, Metrics]:
    metrics = Metrics()
    cfg = world.cfg(scenario, sessions={"commands": commands_cfg(**commands)}, hooks=hooks or {})
    m = world.manager(cfg, metrics=metrics)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    return m, sid, metrics


def inputs(world: World, sid: str) -> list[dict]:
    return jsonl(world.session_dir(sid) / "config" / "fake-inputs.jsonl")


def keys(world: World, sid: str) -> list[dict]:
    """ESC keys the fake received (design D16), kept apart from the typed lines."""
    return jsonl(world.session_dir(sid) / "config" / "fake-keys.jsonl")


def types(world: World, sid: str, prefix: str = "command_") -> list[str]:
    return [e["type"] for e in world.events(sid) if e["type"].startswith(prefix)]


def of_type(world: World, sid: str, type_: str) -> list[dict]:
    return [e["data"] for e in world.events(sid) if e["type"] == type_]


async def finished(world: World, rec: Any, timeout: float = 15.0) -> None:
    await world.until(lambda: rec.state in ("completed", "failed", "cancelled"), timeout)


# ---------------------------------------------------------------- World: real PTY, fake claude

TOKEN = "tok-0123456789abcdef"


async def test_idle_compact_runs_and_completes(world):
    m, sid, metrics = await start(world)
    router = m.router(sid)
    rec = router.request_command("compact", "keep the plan", origin="client")
    assert rec.state == "queued" and rec.turn_id is None and rec.origin == "client"
    await finished(world, rec)
    assert rec.state == "completed" and rec.result == "ok" and rec.error is None
    assert rec.started_at and rec.finished_at
    await world.until(lambda: router.session.state == "ready")
    # One compaction is one `compacted`, and it lands between started and completed.
    order = ("command_queued", "command_started", "compacted", "command_completed")
    seen = [e["type"] for e in world.events(sid) if e["type"] in order]
    assert seen == list(order)
    (done,) = of_type(world, sid, "command_completed")
    assert done["record"]["id"] == rec.id and done["record"]["state"] == "completed"
    (queued,) = of_type(world, sid, "command_queued")
    assert queued["record"]["state"] == "queued"
    (compacted,) = of_type(world, sid, "compacted")
    assert compacted["trigger"] == "manual"
    typed = [i for i in inputs(world, sid) if i["text"].startswith("/")]
    assert typed == [{"text": "/compact keep the plan", "state": "idle", "ts": typed[0]["ts"]}]
    assert sample(metrics, "ccw_compactions_total", trigger="manual") == 1


async def test_compact_boundary_only_completes(world):
    m, sid, _ = await start(world, {"commands": {"compact": {"post_compact_hook": False}}})
    rec = m.router(sid).request_command("compact", origin="agent")
    await finished(world, rec)
    assert (rec.state, rec.result) == ("completed", "ok")
    order = ("command_started", "compacted", "command_completed")
    assert [e["type"] for e in world.events(sid) if e["type"] in order] == list(order)


@pytest.mark.parametrize(
    ("outcome", "text"),
    [
        ("not_enough", "Not enough messages to compact."),
        ("canceled", "Compaction canceled."),
        ("error", "Error compacting conversation"),
    ],
)
async def test_compact_failure_reported(world, outcome, text):
    m, sid, _ = await start(world, {"commands": {"compact": {"outcome": outcome}}})
    rec = m.router(sid).request_command("compact", origin="client")
    await finished(world, rec)
    assert (rec.state, rec.error) == ("failed", "compact_failed")
    assert text in (rec.output or "")
    assert types(world, sid) == ["command_queued", "command_started", "command_failed"]
    (failed,) = of_type(world, sid, "command_failed")
    assert failed["record"]["error"] == "compact_failed" and text in failed["record"]["output"]


@pytest.mark.parametrize(
    ("stdout", "result", "output"),
    [
        (None, "ok", "Reloaded: 0 plugins, 0 skills, 0 agents, 0 hooks, 0 MCP servers"),
        (
            "MCP servers changed. Run /reload-plugins --force to apply.",
            "skipped_cache",
            "MCP servers changed. Run /reload-plugins --force to apply.",
        ),
    ],
)
async def test_reload_plugins_output_and_skipped_cache(world, stdout, result, output):
    scenario = {"commands": {"reload-plugins": {"stdout": stdout}}} if stdout else None
    m, sid, _ = await start(world, scenario)
    rec = m.router(sid).request_command("reload-plugins", origin="agent")
    await finished(world, rec)
    assert (rec.state, rec.result, rec.output) == ("completed", result, output)
    assert [i["text"] for i in inputs(world, sid)] == ["/reload-plugins"]


async def test_other_commands_complete_with_output(world):
    m, sid, _ = await start(world)
    router = m.router(sid)
    skills = router.request_command("reload-skills", origin="client")
    mcp = router.request_command("mcp", "reconnect github", origin="client")
    context = router.request_command("context", origin="client")
    for rec in (skills, mcp, context):
        await finished(world, rec)
    assert [(r.state, r.result, r.output) for r in (skills, mcp, context)] == [
        ("completed", "ok", "Reloaded skills"),
        ("completed", "ok", "Reconnected to github"),
        ("completed", "ok", "Context usage: 0 tokens"),
    ]
    typed = [i["text"] for i in inputs(world, sid)]
    assert typed == ["/reload-skills", "/mcp reconnect github", "/context"]


async def test_multiline_args_typed_as_one_paste(world):
    m, sid, _ = await start(world)
    router = m.router(sid)
    writes: list[bytes] = []
    pty = router.session.pty
    write_locked = pty._write_locked

    async def recording(data: bytes) -> None:
        writes.append(bytes(data))
        await write_locked(data)

    pty._write_locked = recording
    rec = router.request_command("compact", "keep\r\nthe plan\n", origin="client")
    assert rec.args == "keep\nthe plan"
    await finished(world, rec)
    assert (rec.state, rec.result) == ("completed", "ok")
    assert writes == [paste_bytes("/compact keep\nthe plan"), b"\r"]
    assert [i["text"] for i in inputs(world, sid)] == ["/compact keep\nthe plan"]
    (typed,) = [e for e in of_type(world, sid, "user_message") if e["origin"] == "command"]
    assert typed["text"] == "/compact keep\nthe plan"


async def test_stdout_entry_as_user_line_and_output_cut(world):
    long = "x" * 5000
    m, sid, _ = await start(world, {"commands": {"context": {"stdout": long, "as_user": True}}})
    rec = m.router(sid).request_command("context", origin="client")
    await finished(world, rec)
    assert rec.state == "completed" and rec.output == "x" * 4000


async def test_command_timeout_sends_esc(world):
    m, sid, _ = await start(
        world,
        {"commands": {"reload-skills": {"hang": True}}},
        timeout_seconds={"compact": 5, "reload-skills": 0.6, "default": 5},
    )
    router = m.router(sid)
    sent: list[bytes] = []
    pty = router.session.pty
    write = pty.write

    async def recording(data: bytes) -> None:
        sent.append(bytes(data))
        await write(data)

    pty.write = recording
    rec = router.request_command("reload-skills", origin="client")
    await finished(world, rec)
    assert (rec.state, rec.error) == ("failed", "timeout")
    assert types(world, sid) == ["command_queued", "command_started", "command_failed"]
    assert sent.count(b"\x1b") == 1
    await world.until(lambda: router.session.state == "ready")
    # The ESC released the hung command: the next one is typed at an idle prompt and completes.
    nxt = router.request_command("context", origin="client")
    await finished(world, nxt)
    assert nxt.state == "completed"
    assert sent.count(b"\x1b") == 1
    assert [i["state"] for i in inputs(world, sid)] == ["idle", "idle"]


async def test_on_command_deny_vetoes(world):
    deny = {"type": "command", "run": 'echo \'{"decision": "deny", "reason": "not now"}\''}
    m, sid, _ = await start(world, hooks={"onCommand": [{**deny, "blocking": True}]})
    router = m.router(sid)
    rec = router.request_command("compact", origin="client")
    await finished(world, rec)
    assert (rec.state, rec.error, rec.output) == ("failed", "vetoed", "not now")
    assert rec.started_at is None
    assert types(world, sid) == ["command_queued", "command_failed"]
    (failed,) = of_type(world, sid, "command_failed")
    assert failed["record"]["error"] == "vetoed" and failed["record"]["output"] == "not now"
    await world.until(lambda: router.session.state == "ready")
    await asyncio.sleep(0.3)  # anything typed would have reached the fake by now
    assert inputs(world, sid) == []


async def test_on_command_complete_fired_with_record(world, tmp_path):
    seen, done = tmp_path / "on-command.json", tmp_path / "complete.json"
    hooks = {
        "onCommand": [{"type": "command", "run": f"cat > {seen}", "blocking": True}],
        "onCommandComplete": [{"type": "command", "run": f"cat > {done}", "blocking": True}],
    }
    m, sid, _ = await start(world, hooks=hooks)
    rec = m.router(sid).request_command("compact", "be brief", origin="agent")
    await finished(world, rec)
    await world.until(lambda: done.exists() and done.read_text())
    payload = json.loads(seen.read_text())
    assert payload["event"] == "onCommand"
    assert payload["data"] == {
        "command_id": rec.id,
        "name": "compact",
        "args": "be brief",
        "origin": "agent",
        "turn_id": None,
    }
    complete = json.loads(done.read_text())
    assert complete["event"] == "onCommandComplete"
    assert complete["data"] == rec.model_dump(mode="json")
    assert complete["data"]["args"] == "be brief"
    assert complete["data"]["state"] == "completed" and complete["data"]["result"] == "ok"
    assert complete["data"]["finished_at"]


async def test_on_command_complete_fired_for_a_veto(world, tmp_path):
    deny = {"type": "command", "run": 'echo \'{"decision": "deny", "reason": "not now"}\''}
    done = tmp_path / "complete.json"
    hooks = {
        "onCommand": [{**deny, "blocking": True}],
        "onCommandComplete": [{"type": "command", "run": f"cat > {done}", "blocking": True}],
    }
    m, sid, _ = await start(world, hooks=hooks)
    rec = m.router(sid).request_command("compact", origin="client")
    await finished(world, rec)
    await world.until(lambda: done.exists() and done.read_text())
    assert json.loads(done.read_text())["data"] == rec.model_dump(mode="json")


async def test_commands_run_in_sequence(world):
    m, sid, _ = await start(world, {"commands": {"reload-skills": {"delay_seconds": 0.6}}})
    router = m.router(sid)
    first = router.request_command("reload-skills", origin="client")
    second = router.request_command("context", origin="client")
    dup = router.request_command("context", origin="client")
    third = router.request_command("compact", origin="client")
    assert dup is second
    for rec in (first, second, third):
        await finished(world, rec)
    assert [r.state for r in (first, second, third)] == ["completed"] * 3
    await world.until(lambda: router.session.state == "ready")
    seq = [
        (e["type"], e["data"]["record"]["id"])
        for e in world.events(sid)
        if e["type"] in ("command_started", "command_completed", "command_failed")
    ]
    ids = [first.id, second.id, third.id]
    assert seq == [(t, i) for i in ids for t in ("command_started", "command_completed")]
    assert len(of_type(world, sid, "command_queued")) == 3  # the duplicate emitted nothing
    typed = inputs(world, sid)
    assert [i["text"] for i in typed] == ["/reload-skills", "/context", "/compact"]
    # busy held for the whole run: no ready between the first start and the last completion
    evs = world.events(sid)
    begin = next(i for i, e in enumerate(evs) if e["type"] == "command_started")
    after = [e["data"]["state"] for e in evs[begin:] if e["type"] == "session_state"]
    assert after == ["ready"]


async def test_idle_command_marks_busy_and_turn_409(world):
    metrics = Metrics()
    cfg = world.cfg(
        {"commands": {"reload-skills": {"delay_seconds": 0.8}}},
        server={"auth_token": TOKEN},
        sessions={"commands": commands_cfg()},
    )
    m = world.manager(cfg, metrics=metrics)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    router = m.router(sid)
    app = create_app(m, cfg, metrics)
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        rec = router.request_command("reload-skills", origin="client")
        # D4.8: busy at once, without a turn record, so client turns get 409 busy.
        assert router.session.state == "busy" and router.session.current_turn is None
        assert m.get(sid)["state"] == "busy"
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "go"})
        assert r.status_code == 409 and r.json()["error"]["code"] == "busy"
        await finished(world, rec)
        await world.until(lambda: router.session.state == "ready")
        assert m.get(sid)["state"] == "ready"
        states = [d["state"] for d in of_type(world, sid, "session_state")]
        assert states[-2:] == ["busy", "ready"]
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "hello"})
        assert r.status_code == 202
    assert [i["text"] for i in inputs(world, sid) if i["text"].startswith("/")] == [
        "/reload-skills"
    ]


async def test_never_typed_while_busy(world):
    scenario = {
        "turns": [{"match": "go", "steps": [{"sleep": 3.0}, {"text": "done"}]}],
        "commands": {"reload-skills": {"delay_seconds": 0.5}},
    }
    m, sid, _ = await start(world, scenario)
    router = m.router(sid)
    # Idle: two commands queued together, and a turn attempted while they run.
    first = router.request_command("reload-skills", origin="client")
    second = router.request_command("context", origin="client")
    with pytest.raises(TurnRejected):
        await router.submit_turn("go")
    for rec in (first, second):
        await finished(world, rec)
    await world.until(lambda: router.session.state == "ready")
    # A turn runs; a command requested mid-turn is not typed while the turn is busy.
    await router.submit_turn("go")
    await world.until(lambda: router.session.state == "busy")
    during = router.request_command("compact", origin="client")
    assert during.state == "queued"
    await asyncio.sleep(0.6)
    # The turn (3s) must still be in flight, else the window proves nothing.
    assert router.session.state == "busy"
    assert during.state == "queued"
    await world.wait_turn(m, sid)
    await world.until(lambda: router.session.state == "ready")
    # Back at idle: the next command (and the turn-bound one ahead of it) run at the prompt.
    after = router.request_command("reload-plugins", origin="client")
    await finished(world, after)
    await world.until(lambda: router.session.state == "ready")
    typed = inputs(world, sid)
    lines = [i for i in typed if i["text"].startswith("/")]
    assert [i["text"] for i in lines][:2] == ["/reload-skills", "/context"]
    assert "/reload-plugins" in [i["text"] for i in lines]
    assert lines and all(i["state"] == "idle" for i in lines)


async def test_command_metrics_counted(world):
    m, sid, metrics = await start(
        world,
        {"commands": {"reload-skills": {"hang": True}}},
        timeout_seconds={"compact": 5, "reload-skills": 0.6, "default": 5},
    )
    router = m.router(sid)
    recs = [
        router.request_command("compact", origin="agent"),
        router.request_command("context", origin="client"),
        router.request_command("reload-skills", origin="client"),
    ]
    for rec in recs:
        await finished(world, rec)
    await world.until(lambda: router.session.state == "ready")
    counted = {
        ("compact", "agent", "completed"): 1,
        ("context", "client", "completed"): 1,
        ("reload-skills", "client", "failed"): 1,
        ("context", "client", "failed"): 0,
    }
    for (command, origin, result), n in counted.items():
        labels = {"command": command, "origin": origin, "result": result}
        assert sample(metrics, "ccw_commands_total", **labels) == n
    for command in ("compact", "context", "reload-skills"):
        assert sample(metrics, "ccw_command_duration_seconds_count", command=command) == 1
        assert sample(metrics, "ccw_command_duration_seconds_sum", command=command) > 0
    assert sample(metrics, "ccw_command_duration_seconds_sum", command="reload-skills") >= 0.6


async def test_command_queued_during_turn_waits_for_the_stop_gate(world):
    scenario = {"turns": [{"match": "go", "steps": [{"sleep": 1.0}, {"text": "done"}]}]}
    m, sid, _ = await start(world, scenario)
    router = m.router(sid)
    turn = await router.submit_turn("go")
    await world.until(lambda: router.session.state == "busy")
    rec = router.request_command("compact", origin="client")
    assert rec.turn_id == turn.turn_id and rec.state == "queued"
    await asyncio.sleep(0.6)  # past quiet_seconds: the runner did not start mid-turn
    assert rec.state == "queued"
    assert "command_started" not in types(world, sid)
    assert all(not i["text"].startswith("/") for i in inputs(world, sid))


async def test_rejections_propagate_and_emit_nothing(world):
    m, sid, _ = await start(world, agent=[])
    router = m.router(sid)
    with pytest.raises(CommandRejected) as exc:
        router.request_command("compact", origin="agent")
    assert exc.value.code == "not_allowed"
    with pytest.raises(CommandRejected) as exc:
        router.request_command("compact", "keep\x1b[201~", origin="client")
    assert exc.value.code == "invalid_command"
    with pytest.raises(CommandRejected) as exc:
        router.request_command("reload-plugins", "--force", origin="client")
    assert exc.value.code == "force_not_allowed"
    assert types(world, sid) == []
    assert router.session.state == "ready"


async def test_force_allowed_is_typed(world):
    m, sid, _ = await start(world, allow_force=True)
    rec = m.router(sid).request_command("reload-plugins", "--force", origin="client")
    await finished(world, rec)
    assert rec.state == "completed"
    assert [i["text"] for i in inputs(world, sid)] == ["/reload-plugins --force"]


#: The real /reload-plugins stdout (probe 2026-10-29, claude 2.1.304), U+00B7 separators.
REAL_RELOAD = (
    "Reloaded: 0 plugins \u00b7 0 skills \u00b7 6 agents \u00b7 0 hooks \u00b7 "
    "0 plugin MCP servers \u00b7 0 plugin LSP servers"
)
#: Long enough that a command failing on its timeout instead of its signal shows.
SLOW = {"compact": 30, "default": 30}


async def test_reload_plugins_from_client_completes_and_frees_slot(world):
    """G3 regression pin: the reported /reload-plugins from a client completes, the slot frees."""
    cfg = world.cfg(
        {"commands": {"reload-plugins": {"stdout": REAL_RELOAD}}},
        server={"auth_token": TOKEN},
        sessions={"commands": commands_cfg(client=["*"], timeout_seconds=SLOW)},
    )
    m = world.manager(cfg)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    app = create_app(m, cfg)
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        r = await c.post(f"/v1/sessions/{sid}/commands", json={"name": "reload-plugins"})
        assert r.status_code == 202
        url = f"/v1/sessions/{sid}/commands/{r.json()['id']}"
        deadline = time.monotonic() + 5
        while (rec := (await c.get(url)).json())["state"] in ("queued", "running"):
            assert time.monotonic() < deadline, f"command still {rec['state']} after 5 s"
            await asyncio.sleep(0.1)
        assert (rec["state"], rec["result"], rec["output"]) == ("completed", "ok", REAL_RELOAD)
        assert rec["output"].startswith("Reloaded: ")
        assert types(world, sid) == ["command_queued", "command_started", "command_completed"]
        deadline = time.monotonic() + 5
        while (
            t := await c.post(f"/v1/sessions/{sid}/turns", json={"text": "hello"})
        ).status_code != 202:
            assert t.status_code == 409 and time.monotonic() < deadline, t.text
            await asyncio.sleep(0.1)
    assert [i["text"] for i in inputs(world, sid)][:1] == ["/reload-plugins"]
    assert keys(world, sid) == []


async def test_context_with_system_echo_completes(world):
    m, sid, _ = await start(
        world, {"commands": {"context": {"echo": "system"}}}, timeout_seconds=SLOW
    )
    rec = m.router(sid).request_command("context", origin="client")
    await finished(world, rec, timeout=5)
    assert (rec.state, rec.result, rec.output) == ("completed", "ok", "Context usage: 0 tokens")


async def unknown_fails_fast(world: World, scenario: dict | None, name: str, args: str) -> None:
    m, sid, _ = await start(world, scenario, client=["*"], timeout_seconds=SLOW)
    router = m.router(sid)
    begun = time.monotonic()
    rec = router.request_command(name, args, origin="client")
    await finished(world, rec, timeout=5)
    assert time.monotonic() - begun < 5
    assert (rec.state, rec.result, rec.error) == ("failed", None, "unknown_command")
    assert types(world, sid) == ["command_queued", "command_started", "command_failed"]
    (failed,) = of_type(world, sid, "command_failed")
    assert failed["record"]["error"] == "unknown_command"
    await world.until(lambda: router.session.state == "ready", 5)
    await asyncio.sleep(0.3)  # an ESC sent after the failure would have reached the fake by now
    assert keys(world, sid) == []
    line = f"/{name} {args}".rstrip()
    assert inputs(world, sid) == [
        {"text": line, "state": "idle", "ts": inputs(world, sid)[0]["ts"]}
    ]


async def test_unknown_command_fails_fast_without_esc(world):
    """Real sample: summaries/outC.txt:15-16, `/nonexistent some args`."""
    await unknown_fails_fast(world, None, "nonexistent", "some args")


async def test_unknown_command_with_suggestion_fails_fast(world):
    await unknown_fails_fast(world, {"suggest": {"comapct": "compact"}}, "comapct", "")


async def test_alias_echo_completes(world):
    """Real sample: summaries/outC.txt:32, /cost echoes /usage as a system line."""
    cost = {"echo": "system", "echo_name": "usage", "stdout": "Total cost: $0.0000"}
    m, sid, _ = await start(world, {"commands": {"cost": cost}}, client=["*"], timeout_seconds=SLOW)
    rec = m.router(sid).request_command("cost", origin="client")
    await finished(world, rec, timeout=5)
    assert (rec.state, rec.result, rec.output) == ("completed", "ok", "Total cost: $0.0000")


# ---------------------------------------------------------------- unit: scripted session


class CommandStub(StubSession):
    """StubSession plus the command surface; `script` answers each typed line on the bus."""

    def __init__(self, workdir: Path, transcript: Path) -> None:
        super().__init__(workdir, transcript)
        self.router: Any = None
        self.typed: list[str] = []
        self.escapes = 0
        self.script: dict[str, list[tuple[str, dict]]] = {}
        self.quiet_waits: list[float] = []
        self.command_active = False

    def begin_command(self) -> bool:
        if self.state != "ready" or self.turn is not None:
            return False
        self.command_active = True
        self.state = "busy"
        return True

    def end_command(self) -> None:
        self.command_active = False
        if self.state == "busy" and self.turn is None:
            self.state = "ready"

    async def wait_quiet(self, seconds: float, timeout: float) -> bool:
        self.quiet_waits.append(seconds)
        return True

    async def send_command(self, line: str) -> None:
        self.typed.append(line)
        for kind, data in self.script.get(line, []):
            self.router.signals.publish(kind, data)

    async def send_escape(self) -> None:
        self.escapes += 1


@pytest.fixture
async def u(tmp_path):
    h = Harness(tmp_path, sessions={"commands": commands_cfg(quiet_seconds=0.3)})
    stub = CommandStub(h.workdir, h.transcript)
    stub.router = h.router
    h.router.bind(stub)  # type: ignore[arg-type]
    h.session = stub
    h.router._metrics = Metrics()
    yield h
    await h.router.close()


async def run(u: Any, name: str, args: str = "") -> Any:
    rec = u.router.request_command(name, args, origin="client")
    await asyncio.wait_for(u.router.wait_runner_idle(), 5)
    return rec


async def test_stderr_only_output_is_command_error(u):
    u.session.script["/reload-skills"] = [
        ("command_invoked", {"name": "reload-skills", "args": ""}),
        ("command_output", {"stdout": "", "stderr": "boom"}),
    ]
    rec = await run(u, "reload-skills")
    assert (rec.state, rec.error, rec.output) == ("failed", "command_error", "boom")
    assert u.session.quiet_waits == [0.3]
    assert u.session.state == "ready"


async def test_output_before_its_echo_is_not_the_result(u):
    u.session.script["/context"] = [
        ("command_output", {"stdout": "stale", "stderr": ""}),
        ("command_invoked", {"name": "context", "args": ""}),
        ("command_output", {"stdout": "Context usage: 1 tokens", "stderr": ""}),
    ]
    rec = await run(u, "context")
    assert (rec.state, rec.output) == ("completed", "Context usage: 1 tokens")


async def test_any_echo_after_the_mark_completes(u):
    """claude echoes the resolved alias (/cost echoes /usage): the typed name is not matched."""
    u.router.commands_cfg.client = ["*"]
    u.session.script["/cost"] = [
        ("command_invoked", {"name": "usage", "args": ""}),
        ("command_output", {"stdout": "Total cost: $0.0000", "stderr": ""}),
    ]
    rec = await run(u, "cost")
    assert (rec.state, rec.output) == ("completed", "Total cost: $0.0000")


@pytest.mark.parametrize("name", ["nonexistent", "compact"])
async def test_unknown_signal_fails_at_once_without_esc(u, name):
    u.router.commands_cfg.client = ["*"]
    u.session.script[f"/{name}"] = [("command_unknown", {"name": name})]
    begun = time.monotonic()
    rec = await run(u, name)
    assert time.monotonic() - begun < 1
    assert (rec.state, rec.result, rec.error, rec.output) == (
        "failed",
        None,
        "unknown_command",
        None,
    )
    assert u.session.escapes == 0 and u.session.state == "ready"


async def test_compact_ignores_auto_trigger_and_signals_before_mark(u):
    u.router.signals.publish("post_compact", {"trigger": "manual"})  # before the mark
    u.session.script["/compact"] = [
        ("post_compact", {"trigger": "auto"}),
        ("compact_boundary", {"trigger": "manual"}),
    ]
    rec = await run(u, "compact")
    assert (rec.state, rec.result) == ("completed", "ok")


async def test_timeout_in_unit_sends_one_esc(u):
    u.router.commands_cfg.timeout_seconds = {"compact": 5, "default": 0.2}
    rec = await run(u, "context")
    assert (rec.state, rec.error) == ("failed", "timeout")
    assert u.session.escapes == 1 and u.session.typed == ["/context"]
    assert u.session.state == "ready"


async def test_busy_with_a_turn_queues_with_its_turn_id(u):
    await u.router.submit_turn("go")
    u.session.state = "busy"
    rec = u.router.request_command("compact", origin="client")
    assert rec.turn_id == u.session.turn.turn_id
    await asyncio.sleep(0.1)
    assert rec.state == "queued" and u.session.typed == []
    assert [e.type for e in u.events() if e.type.startswith("command_")] == ["command_queued"]


async def test_command_while_booting_is_rejected_invalid_state(u):
    u.session.state = "booting"
    with pytest.raises(CommandRejected) as err:
        u.router.request_command("context", origin="client")
    assert err.value.code == "invalid_state"
    assert u.router.commands.list() == [] and not u.router.runner_active


async def test_screen_never_quiet_fails_without_typing(u):
    async def never(seconds: float, timeout: float) -> bool:
        return False

    u.session.wait_quiet = never
    rec = await run(u, "context")
    assert (rec.state, rec.error) == ("failed", "timeout")
    assert u.session.typed == [] and u.session.escapes == 0
    assert [e.type for e in u.events() if e.type.startswith("command_")] == [
        "command_queued",
        "command_failed",
    ]


async def test_write_failure_fails_the_command_and_frees_the_slot(u):
    from claude_code_wrapper.pty_proc import PtyClosed

    async def closed(line: str) -> None:
        raise PtyClosed("gone")

    u.session.send_command = closed
    rec = await run(u, "context")
    assert (rec.state, rec.error) == ("failed", "claude_exited")
    assert u.session.state == "ready"


async def test_unexpected_error_fails_the_command_and_runs_the_next(u):
    calls: list[str] = []

    async def boom(line: str) -> None:
        calls.append(line)
        if line == "/context":
            raise RuntimeError("kaboom")
        u.router.signals.publish("compact_boundary", {"trigger": "manual"})

    u.session.send_command = boom
    first = u.router.request_command("context", origin="client")
    second = u.router.request_command("compact", origin="client")
    await asyncio.wait_for(u.router.wait_runner_idle(), 5)
    assert (first.state, first.error) == ("failed", "internal_error")
    assert second.state == "completed" and calls == ["/context", "/compact"]
    assert any("kaboom" in e.data["message"] for e in u.events("error"))


async def test_cancelled_during_quiet_wait_fires_no_hook_and_types_nothing(tmp_path):
    from test_router import webhook

    h = Harness(tmp_path, hooks={"onCommand": [webhook()]}, sessions={"commands": commands_cfg()})
    stub = CommandStub(h.workdir, h.transcript)
    stub.router = h.router
    h.router.bind(stub)  # type: ignore[arg-type]

    async def cancel_meanwhile(seconds: float, timeout: float) -> bool:
        h.router.commands.cancel_queued()
        return True

    stub.wait_quiet = cancel_meanwhile
    try:
        rec = h.router.request_command("context", origin="client")
        await asyncio.wait_for(h.router.wait_runner_idle(), 5)
        assert rec.state == "cancelled"
        assert h.hook_posts == [] and stub.typed == [] and stub.state == "ready"
    finally:
        await h.router.close()
