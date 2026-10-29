"""Dialog commands dismissed by ESC inside one deadline per command (design D7, Bounds).

Real samples the fake matches: summaries/outA.txt (/model and /config write their echo only after
ESC, /config needs two ESCs, a paste into an open dialog acts on it) and outC.txt (/usage:
"Settings dialog dismissed"), both under /workdir/workdir/.scratchpad/probe-slash.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

import pytest
from pydantic import ValidationError
from test_command_runner import (
    CommandStub,
    commands_cfg,
    finished,
    inputs,
    keys,
    of_type,
    start,
)
from test_router import Harness

from claude_code_wrapper.commands import effective_commands
from claude_code_wrapper.config import CommandsConfig, CommandsOverride
from claude_code_wrapper.manager import CreateSessionRequest

DEFAULT_PATTERNS = [
    "esc to cancel",
    "esc to close",
    "esc to clear",
    "esc to exit",
    "enter to confirm",
]
#: Long enough that a command failing on its timeout instead of its dialog leg shows.
SLOW = {"compact": 30, "default": 30}


def ts(row: dict) -> float:
    return datetime.fromisoformat(row["ts"]).timestamp()


def gaps(rows: list[dict]) -> list[float]:
    return [ts(b) - ts(a) for a, b in zip(rows, rows[1:], strict=False)]


def dialog(escs: Any, footer: str = "Esc to cancel", stdout: str = "Help dialog dismissed") -> dict:
    return {"dialog": {"footer": footer, "dismiss_stdout": stdout, "escs": escs}}


def typed(world: Any, sid: str, line: str) -> dict:
    (row,) = [i for i in inputs(world, sid) if i["text"] == line]
    return row


async def test_dialog_command_dismissed_by_escape(world):
    m, sid, _ = await start(
        world, {"commands": {"help": dialog(1)}}, client=["*"], timeout_seconds=SLOW
    )
    router = m.router(sid)
    rec = router.request_command("help", origin="client")
    await finished(world, rec, timeout=20)
    assert (rec.state, rec.result, rec.error, rec.output) == (
        "completed",
        "dismissed",
        None,
        "Help dialog dismissed",
    )
    (esc,) = keys(world, sid)
    # The footer is printed right after the line is submitted: the ESC waited out the grace.
    assert ts(esc) - ts(typed(world, sid, "/help")) >= 2.0
    await world.until(lambda: router.session.state == "ready", 5)
    await asyncio.sleep(0.3)
    assert len(keys(world, sid)) == 1


async def test_config_dialog_needs_two_escapes(world):
    scenario = {"commands": {"config": dialog(2, "Esc to clear", "Disabled auto-compact")}}
    m, sid, _ = await start(
        world, scenario, client=["*"], timeout_seconds=SLOW, dialog_grace_seconds=0.5
    )
    rec = m.router(sid).request_command("config", origin="client")
    await finished(world, rec, timeout=20)
    assert (rec.state, rec.result, rec.output) == (
        "completed",
        "dismissed",
        "Disabled auto-compact",
    )
    got = keys(world, sid)
    assert len(got) == 2
    assert gaps(got)[0] >= 1.5


async def test_dialog_stuck_after_esc_budget(world):
    m, sid, _ = await start(
        world,
        {"commands": {"help": dialog("never")}},
        client=["*"],
        timeout_seconds=SLOW,
        dialog_grace_seconds=0.3,
    )
    rec = m.router(sid).request_command("help", origin="client")
    await finished(world, rec, timeout=30)
    assert (rec.state, rec.result, rec.error) == ("failed", None, "dialog_stuck")
    got = keys(world, sid)
    assert len(got) == 3
    assert all(g >= 1.0 for g in gaps(got))
    (failed,) = of_type(world, sid, "command_failed")
    assert failed["record"]["error"] == "dialog_stuck"


@pytest.mark.parametrize("leg", ["dialog_stuck", "timeout"])
async def test_open_dialog_restarts_claude_before_release(world, leg):
    if leg == "dialog_stuck":
        kw: dict[str, Any] = {"timeout_seconds": SLOW, "dialog_grace_seconds": 0.3}
    else:  # the deadline comes before the grace: the timeout leg finds the dialog on screen
        kw = {"timeout_seconds": {"compact": 30, "default": 30, "help": 1.0}}
    m, sid, _ = await start(world, {"commands": {"help": dialog("never")}}, client=["*"], **kw)
    router = m.router(sid)
    claude_sid = world.meta(sid)["claude_session_id"]
    await router.submit_turn("hello")  # writes the transcript, so the relaunch resumes it
    await world.wait_turn(m, sid)
    await world.until(lambda: router.session.state == "ready", 5)
    rec = router.request_command("help", origin="client")
    await finished(world, rec, timeout=40)
    assert (rec.state, rec.error) == ("failed", leg)
    # claude was relaunched with --resume of the same claude session before the slot was released
    first, again = world.interactive_argvs()
    assert first[first.index("--session-id") + 1] == claude_sid
    assert again[again.index("--resume") + 1] == claude_sid
    events = world.events(sid)
    started = next(e["seq"] for e in events if e["type"] == "command_started")
    ended = next(e["seq"] for e in events if e["type"] == "command_failed")
    between = [
        e["data"] for e in events if e["type"] == "session_state" and started < e["seq"] < ended
    ]
    assert [d["restart"] for d in between if "restart" in d] == [{"count": 0, "resumed": True}]
    assert "ready" not in [d["state"] for d in between]
    await world.until(lambda: router.session.state == "ready", 10)
    assert router.session.restarts == 0 and m.get(sid)["restarts"] == 0
    # The next prose turn is typed into the fresh prompt, never into the dialog.
    after = await router.submit_turn("after the dialog")
    await world.until(lambda: after.state == "completed", 10)
    rows = inputs(world, sid)
    assert [r["text"].split("\n")[0] for r in rows] == ["hello", "/help", "after the dialog"]
    assert {r["state"] for r in rows} == {"idle"}


async def test_timeout_second_escape_only_with_dialog(world):
    scenario = {"commands": {"reload-skills": {"hang": True}, "help": dialog("never")}}
    m, sid, _ = await start(
        world, scenario, client=["*"], timeout_seconds={"compact": 30, "default": 1.0}
    )
    router = m.router(sid)
    plain = router.request_command("reload-skills", origin="client")
    await finished(world, plain, timeout=10)
    assert (plain.state, plain.error) == ("failed", "timeout")
    assert len(keys(world, sid)) == 1
    assert len(world.interactive_argvs()) == 1  # no dialog on screen: claude is not restarted
    await world.until(lambda: router.session.state == "ready", 5)
    taken = time.time()
    shown = router.request_command("help", origin="client")
    await finished(world, shown, timeout=30)
    assert (shown.state, shown.error) == ("failed", "timeout")
    got = keys(world, sid)[1:]
    assert len(got) == 2  # the dialog stayed on screen after the first ESC
    assert gaps(got)[0] >= 1.5
    assert ts(got[0]) - taken >= 1.0  # the deadline's ESC, not the dialog leg's (grace 2 s)


async def test_one_deadline_covers_quiet_wait_and_completion(tmp_path):
    h = Harness(tmp_path, sessions={"commands": commands_cfg(timeout_seconds={"default": 3})})
    stub = CommandStub(h.workdir, h.transcript)
    stub.router = h.router
    h.router.bind(stub)  # type: ignore[arg-type]
    quiet_timeouts: list[float] = []

    async def busy_screen(seconds: float, timeout: float) -> bool:
        quiet_timeouts.append(timeout)
        await asyncio.sleep(min(timeout, 2.5))  # the screen keeps changing for most of it
        return True

    stub.wait_quiet = busy_screen
    try:
        begun = time.monotonic()
        rec = h.router.request_command("context", origin="client")
        await asyncio.wait_for(h.router.wait_runner_idle(), 15)
        took = time.monotonic() - begun
        assert (rec.state, rec.error) == ("failed", "timeout")
        assert took <= 3 + 2, f"took {took:.1f}s: the completion wait spent a second timeout"
        assert quiet_timeouts and quiet_timeouts[0] <= 3
        assert stub.typed == ["/context"] and stub.escapes == 1
    finally:
        await h.router.close()


async def test_grace_restarts_on_output(world):
    slow = {"footer": "Esc to cancel", "delay_seconds": 3.0, "print_every": 0.5, "stdout": "done"}
    m, sid, _ = await start(world, {"commands": {"slow": slow}}, client=["*"], timeout_seconds=SLOW)
    rec = m.router(sid).request_command("slow", origin="client")
    await finished(world, rec, timeout=20)
    assert (rec.state, rec.result, rec.output) == ("completed", "ok", "done")
    assert keys(world, sid) == []


async def test_compact_excluded_from_dialog_detection(world):
    compact = {"outcome": "hang", "footer": "Esc to cancel"}
    m, sid, _ = await start(
        world,
        {"commands": {"compact": compact}},
        timeout_seconds={"compact": 2.0, "default": 30},
        dialog_grace_seconds=0.2,
    )
    taken = time.time()
    rec = m.router(sid).request_command("compact", origin="client")
    await finished(world, rec, timeout=15)
    assert (rec.state, rec.error) == ("failed", "timeout")
    (esc,) = keys(world, sid)  # the timeout ESC only, never a dialog-leg one
    assert ts(esc) - taken >= 2.0
    assert len(world.interactive_argvs()) == 1


async def test_dialog_config_defaults_and_override(world):
    cfg = CommandsConfig()
    assert cfg.dialog_grace_seconds == 2.0
    assert cfg.dialog_patterns == DEFAULT_PATTERNS
    over = CommandsOverride(dialog_grace_seconds=0.5, dialog_patterns=["press q to quit"])
    eff = effective_commands(cfg, over)
    assert (eff.dialog_grace_seconds, eff.dialog_patterns) == (0.5, ["press q to quit"])
    assert effective_commands(cfg, CommandsOverride()).dialog_patterns == DEFAULT_PATTERNS
    for bad in ({"dialog_grace_seconds": -1}, {"dialog_patterns": ["  "]}):
        with pytest.raises(ValidationError):
            CommandsConfig.model_validate(bad)
        with pytest.raises(ValidationError):
            CommandsOverride.model_validate(bad)
    m = world.manager(world.cfg())
    plain = (await m.create(CreateSessionRequest()))["session_id"]
    custom = (
        await m.create(
            CreateSessionRequest(
                commands={"dialog_grace_seconds": 0.5, "dialog_patterns": ["press q to quit"]}
            )
        )
    )["session_id"]
    got = m.router(custom).commands_cfg
    assert (got.dialog_grace_seconds, got.dialog_patterns) == (0.5, ["press q to quit"])
    base = m.router(plain).commands_cfg
    assert (base.dialog_grace_seconds, base.dialog_patterns) == (2.0, DEFAULT_PATTERNS)
