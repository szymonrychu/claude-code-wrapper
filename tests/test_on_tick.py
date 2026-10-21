"""K7 onTick: the router-owned timer hook (interval_seconds, when busy|always)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import World

from claude_code_wrapper.config import HOOK_EVENTS, Config, ConfigError, HooksConfig, load_config
from claude_code_wrapper.hooks import EVENT_DECISIONS
from claude_code_wrapper.manager import CreateSessionRequest


def tick(path: Path, **kw: Any) -> dict[str, Any]:
    return {
        "type": "command",
        "run": f"cat >> {path}; echo >> {path}",
        "interval_seconds": 0.2,
        **kw,
    }


def ticks(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


async def start(world: World, cfg: Config) -> tuple[Any, str]:
    m = world.manager(cfg)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    return m, sid


SLEEP = {"turns": [{"match": "go", "steps": [{"sleep": 30}]}]}


def test_on_tick_config_and_decisions():
    assert "onTick" in HOOK_EVENTS
    assert EVENT_DECISIONS["onTick"] == ("allow",)
    cfg = HooksConfig.model_validate(
        {"onTick": [{"type": "command", "run": "true", "interval_seconds": 5}]}
    )
    assert cfg.onTick[0].interval_seconds == 5 and cfg.onTick[0].when is None


@pytest.mark.parametrize(
    ("yaml", "path"),
    [
        ("onTick: [{type: command, run: x}]", "hooks.onTick.0.command.interval_seconds:"),
        ("onTick: [{type: command, run: x, interval_seconds: 0}]", "interval_seconds"),
        (
            "onTick: [{type: webhook, url: 'http://x/y', interval_seconds: 1, when: x}]",
            "when",
        ),
        (
            "onTurn: [{type: command, run: x, interval_seconds: 1}]",
            "hooks.onTurn.0.command.interval_seconds:",
        ),
        ("onTurn: [{type: command, run: x, when: busy}]", "hooks.onTurn.0.command.when:"),
    ],
)
def test_on_tick_config_errors(tmp_path, yaml, path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"hooks: {{{yaml}}}\n")
    with pytest.raises(ConfigError) as exc:
        load_config(cfg)
    assert path in str(exc.value)


def _err(tmp_path, yaml):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"hooks: {{{yaml}}}\n")
    with pytest.raises(ConfigError) as exc:
        load_config(cfg)
    return str(exc.value)


def test_interval_required_on_on_tick(tmp_path):
    assert "hooks.onTick.0.command.interval_seconds:" in _err(
        tmp_path, "onTick: [{type: command, run: x}]"
    )


def test_interval_and_when_rejected_on_other_events(tmp_path):
    assert "hooks.onTurn.0.command.interval_seconds:" in _err(
        tmp_path, "onTurn: [{type: command, run: x, interval_seconds: 1}]"
    )
    assert "hooks.onTurn.0.command.when:" in _err(
        tmp_path, "onTurn: [{type: command, run: x, when: busy}]"
    )


async def test_always_tick_fires_when_idle(world: World, tmp_path: Path):
    out = tmp_path / "ticks.jsonl"
    m, sid = await start(world, world.cfg(hooks={"onTick": [tick(out, when="always")]}))
    await world.until(lambda: len(ticks(out)) >= 2)
    first = ticks(out)[0]
    assert first["event"] == "onTick"
    assert first["turn_id"] is None
    assert first["data"] == {"turn_id": None, "elapsed_seconds": None, "interval_seconds": 0.2}
    router = m.router(sid)
    await router.close()
    assert not router._tick_tasks or all(t.done() for t in router._tick_tasks)
    count = len(ticks(out))
    await asyncio.sleep(0.6)
    assert len(ticks(out)) == count


async def test_busy_tick_fires_only_during_turn(world: World, tmp_path: Path):
    out = tmp_path / "ticks.jsonl"
    m, sid = await start(world, world.cfg(SLEEP, hooks={"onTick": [tick(out)]}))
    await asyncio.sleep(0.8)
    assert ticks(out) == []
    record = await m.router(sid).submit_turn("go")
    await world.until(lambda: len(ticks(out)) >= 2)
    data = ticks(out)[-1]["data"]
    assert data["turn_id"] == record.turn_id
    assert data["elapsed_seconds"] > 0
    assert data["interval_seconds"] == 0.2


async def test_tick_does_not_overlap_itself(world: World, tmp_path: Path):
    out = tmp_path / "ticks.jsonl"
    slow = tick(out, when="always")
    slow["run"] = f'cat >> {out}; echo >> {out}; sleep 1; echo \'{{"decision":"deny"}}\''
    slow["blocking"] = True
    m, sid = await start(world, world.cfg(hooks={"onTick": [slow]}))
    await world.until(lambda: len(ticks(out)) >= 1)
    await asyncio.sleep(0.7)
    assert len(ticks(out)) == 1
    await world.until(lambda: len(ticks(out)) >= 2)
    assert m.router(sid).session.state == "ready"


async def test_ticks_survive_to_second_action_independently(world: World, tmp_path: Path):
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    hooks = {"onTick": [tick(a, when="always"), tick(b, when="always", interval_seconds=0.3)]}
    await start(world, world.cfg(hooks=hooks))
    await world.until(lambda: len(ticks(a)) >= 2 and len(ticks(b)) >= 1)
    assert {t["data"]["interval_seconds"] for t in ticks(a)} == {0.2}
    assert {t["data"]["interval_seconds"] for t in ticks(b)} == {0.3}


async def test_ticks_stop_on_close(world: World, tmp_path: Path):
    out = tmp_path / "ticks.jsonl"
    slow = tick(out, when="always")
    slow["run"] = f"cat >> {out}; echo >> {out}; sleep 5"
    slow["blocking"] = True
    m, sid = await start(world, world.cfg(hooks={"onTick": [slow]}))
    await world.until(lambda: len(ticks(out)) >= 1)
    router = m.router(sid)
    await router.close()
    assert all(t.done() for t in router._tick_tasks)
    assert all(t.done() for t in router._tick_runs)
    count = len(ticks(out))
    await asyncio.sleep(0.6)
    assert len(ticks(out)) == count


async def test_tick_data_fields(world: World, tmp_path: Path):
    out = tmp_path / "ticks.jsonl"
    m, sid = await start(world, world.cfg(SLEEP, hooks={"onTick": [tick(out)]}))
    record = await m.router(sid).submit_turn("go")
    await world.until(lambda: len(ticks(out)) >= 1)
    data = ticks(out)[-1]["data"]
    assert set(data) == {"turn_id", "elapsed_seconds", "interval_seconds"}
    assert data["turn_id"] == record.turn_id
    assert data["elapsed_seconds"] > 0
    assert data["interval_seconds"] == 0.2
