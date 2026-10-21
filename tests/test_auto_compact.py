"""Idle auto-compact: config, arming, firing rules and the idle reaper interaction (design D6)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from conftest import World
from test_command_runner import commands_cfg

from claude_code_wrapper.config import Config, ConfigError
from claude_code_wrapper.manager import CreateSessionRequest, _epoch

BASE: dict[str, Any] = {"server": {"callback_secret": "cb-secret-0123456789"}}


def parse(sessions: dict[str, Any]) -> Config:
    return Config.model_validate({**BASE, "sessions": sessions})


# ---------------------------------------------------------------- config


def test_defaults_are_off():
    cfg = parse({}).sessions.auto_compact
    assert (cfg.idle_seconds, cfg.instructions, cfg.min_context_tokens) == (0, "", 0)


def test_unknown_key_and_negative_values_rejected():
    for bad in ({"nope": 1}, {"idle_seconds": -1}, {"min_context_tokens": -1}):
        with pytest.raises(ValueError):
            parse({"auto_compact": bad})


def test_instructions_must_be_one_line():
    with pytest.raises(ValueError):
        parse({"auto_compact": {"idle_seconds": 5, "instructions": "a\nb"}})
    with pytest.raises(ValueError):
        parse({"auto_compact": {"idle_seconds": 5, "instructions": "x" * 2001}})
    parse({"auto_compact": {"idle_seconds": 5, "instructions": "keep the plan"}})


def test_idle_seconds_not_below_reaper_is_a_config_error(tmp_path):
    from claude_code_wrapper.config import load_config

    path = tmp_path / "c.yaml"
    path.write_text(
        "server:\n  callback_secret: cb-secret-0123456789\n"
        "sessions:\n  idle_timeout_seconds: 60\n  auto_compact:\n    idle_seconds: 60\n"
    )
    with pytest.raises(ConfigError, match=r"sessions\.auto_compact\.idle_seconds"):
        load_config(path)
    parse({"idle_timeout_seconds": 60, "auto_compact": {"idle_seconds": 59}})
    parse({"idle_timeout_seconds": 0, "auto_compact": {"idle_seconds": 600}})


# ---------------------------------------------------------------- behaviour


async def start(world: World, auto: dict[str, Any], **sessions: Any):
    cfg = world.cfg(
        None,
        sessions={
            "commands": commands_cfg(),
            "auto_compact": {"idle_seconds": 0.3, **auto},
            **sessions,
        },
    )
    m = world.manager(cfg)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    return m, sid


def idle_completions(world: World, sid: str) -> list[dict]:
    return [
        e
        for e in world.events(sid)
        if e["type"] == "command_completed" and e["data"]["record"]["origin"] == "idle"
    ]


async def test_compacts_once_per_idle_gap(world):
    m, sid = await start(world, {"instructions": "keep the plan"})
    router = m.router(sid)
    await asyncio.sleep(0.8)
    assert idle_completions(world, sid) == []  # never ran a turn: never compacted
    await router.submit_turn("go")
    await world.wait_turn(m, sid)
    await world.until(lambda: idle_completions(world, sid), what="auto compact completed")
    rec = idle_completions(world, sid)[0]["data"]["record"]
    assert rec["name"] == "compact" and rec["args"] == {"instructions": "keep the plan"}
    await world.until(lambda: router.session.state == "ready")
    await asyncio.sleep(1.0)
    assert len(idle_completions(world, sid)) == 1  # once per gap
    # The next turn end re-arms it.
    await router.submit_turn("again")
    await world.wait_turn(m, sid)
    await world.until(lambda: len(idle_completions(world, sid)) == 2, what="second gap compact")


async def test_new_turn_cancels_pending_timer(world):
    m, sid = await start(world, {"idle_seconds": 1.0})
    router = m.router(sid)
    await router.submit_turn("go")
    await world.wait_turn(m, sid)
    await asyncio.sleep(0.4)
    await router.submit_turn("again")
    await world.wait_turn(m, sid, "completed")
    await asyncio.sleep(0.5)  # 0.9s since the first turn end, 0.5 since the second
    assert idle_completions(world, sid) == []


async def test_min_context_tokens_skips_small_contexts(world):
    m, sid = await start(world, {"min_context_tokens": 10_000_000})
    router = m.router(sid)
    await router.submit_turn("go")
    await world.wait_turn(m, sid)
    await asyncio.sleep(1.0)
    assert not [e for e in world.events(sid) if e["type"].startswith("command_")]


async def test_idle_commands_do_not_count_as_activity(world):
    m, sid = await start(world, {}, idle_timeout_seconds=300)
    router = m.router(sid)
    await router.submit_turn("go")
    await world.wait_turn(m, sid)
    await world.until(lambda: idle_completions(world, sid), what="auto compact completed")
    await world.until(lambda: router.session.state == "ready")
    await asyncio.sleep(0.3)
    events = world.events(sid)
    ctx = m._active[sid]
    spans = router.idle_ranges()
    assert len(spans) == 1
    real = [e for e in events if not any(lo <= e["seq"] <= hi for lo, hi in spans)]
    completed = next(e for e in events if e["type"] == "command_completed")
    assert real[-1]["seq"] < completed["seq"]  # nothing real after the idle run
    assert m._last_activity(ctx) == pytest.approx(_epoch(real[-1]["ts"]), abs=0.01)
    assert m._last_activity(ctx) < _epoch(events[-1]["ts"])


async def test_client_command_counts_as_activity(world):
    m, sid = await start(world, {"idle_seconds": 0}, idle_timeout_seconds=300)
    router = m.router(sid)
    await router.submit_turn("go")
    await world.wait_turn(m, sid)
    rec = router.request_command("reload-skills", origin="client")
    await world.until(lambda: rec.state == "completed")
    await world.until(lambda: router.session.state == "ready")
    ctx = m._active[sid]
    assert m._last_activity(ctx) >= _epoch(
        next(e["ts"] for e in world.events(sid) if e["type"] == "command_completed")
    )
