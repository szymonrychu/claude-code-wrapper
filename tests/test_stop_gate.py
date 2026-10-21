"""K1 stop gate: the onStop hook event (allow, block, defer) and the background-task defer."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from conftest import World, wait_turn

from claude_code_wrapper.bootstrap.settings import RelayTimeouts, relay_timeouts
from claude_code_wrapper.config import HOOK_EVENTS, Config, HooksConfig
from claude_code_wrapper.events import EVENT_TYPES
from claude_code_wrapper.hooks import EVENT_DECISIONS, HookDispatcher, build_payload
from claude_code_wrapper.manager import CreateSessionRequest
from claude_code_wrapper.redact import Redactor

TASK = {"id": "b1", "type": "local_bash", "status": "running", "description": "npm test"}


class Gate:
    """An onStop command action scripted per invocation: reply files out-<n>[.err] in `dir`.

    Every invocation saves its stdin envelope as `<CCW_STATE_DIR>/stop-<n>.json`. `out-<n>`
    is printed on stdout (exit 0); `out-<n>.err` is printed on stderr with exit 2; neither
    means a plain allow.
    """

    def __init__(self, root: Path) -> None:
        self.dir = root / "gate"
        self.dir.mkdir(exist_ok=True)

    def reply(self, n: int, obj: dict[str, Any]) -> None:
        (self.dir / f"out-{n}").write_text(json.dumps(obj))

    def exit2(self, n: int, stderr: str) -> None:
        (self.dir / f"out-{n}.err").write_text(stderr)

    def action(self, **kw: Any) -> dict[str, Any]:
        run = (
            'd="$CCW_STATE_DIR"; n=$(( $(cat "$d/n" 2>/dev/null || echo 0) + 1 )); '
            'echo "$n" > "$d/n"; cat > "$d/stop-$n.json"; '
            f'f="{self.dir}/out-$n"; '
            'if [ -f "$f.err" ]; then cat "$f.err" >&2; exit 2; fi; '
            'if [ -f "$f" ]; then cat "$f"; fi; exit 0'
        )
        return {"type": "command", "run": run, "blocking": True, **kw}

    @staticmethod
    def calls(world: World, sid: str) -> list[dict[str, Any]]:
        state = world.session_dir(sid) / "hook-state"
        files = sorted(state.glob("stop-*.json"), key=lambda p: int(p.stem.split("-")[1]))
        return [json.loads(p.read_text()) for p in files]


def counter(path: Path) -> dict[str, Any]:
    return {"type": "command", "run": f"cat >> {path}; echo >> {path}", "blocking": True}


def types(world: World, sid: str, *wanted: str) -> list[str]:
    return [e["type"] for e in world.events(sid) if e["type"] in wanted]


def of_type(world: World, sid: str, type_: str) -> list[dict[str, Any]]:
    return [e["data"] for e in world.events(sid) if e["type"] == type_]


async def start(world: World, cfg: Config) -> tuple[Any, str]:
    m = world.manager(cfg)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    return m, sid


def turn(steps: list[dict], **kw: Any) -> dict[str, Any]:
    return {"turns": [{"match": "go", "steps": steps, **kw}]}


# ---------------------------------------------------------------- config, decisions, relay


def test_on_stop_config_and_decisions():
    assert "onStop" in HOOK_EVENTS
    assert EVENT_DECISIONS["onStop"] == ("allow", "block", "defer")
    assert {"turn_continued", "turn_deferred"} <= set(EVENT_TYPES)
    cfg = Config.model_validate({"hooks": {"onStop": [{"type": "command", "run": "true"}]}})
    assert cfg.hooks.for_event("onStop")[0].run == "true"
    assert Config().sessions.defer_stop_on_background_tasks is False
    with pytest.raises(ValueError, match="matcher"):
        HooksConfig.model_validate(
            {"onStop": [{"type": "command", "run": "true", "matcher": "Bash"}]}
        )


def test_relay_timeout_covers_blocking_on_stop():
    def cfg(blocking: bool) -> Config:
        action = {"type": "command", "run": "true", "blocking": blocking, "timeout_seconds": 100}
        return Config.model_validate({"hooks": {"onStop": [action]}})

    assert relay_timeouts(cfg(True)) == RelayTimeouts(105, 51.5)
    assert relay_timeouts(cfg(False)) == RelayTimeouts(30, 14)


def _dispatcher(tmp_path: Path, actions: list[dict]) -> tuple[HookDispatcher, list]:
    results: list = []
    d = HookDispatcher(
        HooksConfig.model_validate({"onStop": actions}),
        Redactor(),
        base_env={"PATH": os.environ["PATH"]},
        default_cwd=tmp_path,
        on_result=results.append,
    )
    return d, results


def _payload() -> dict[str, Any]:
    return build_payload(
        "onStop", session_id="s", claude_session_id="c", turn_id="t", workdir="/w", data={}
    )


def _say(obj: dict) -> dict[str, Any]:
    return {"type": "command", "run": f"printf '%s' '{json.dumps(obj)}'", "blocking": True}


async def test_strictest_decision_wins_with_first_reason(tmp_path):
    d, _ = _dispatcher(
        tmp_path,
        [
            _say({"decision": "allow"}),
            _say({"decision": "defer"}),
            _say({"decision": "block", "reason": "first"}),
            _say({"decision": "block", "reason": "second"}),
        ],
    )
    o = await d.fire("onStop", _payload())
    assert (o.decision, o.reason) == ("block", "first")
    d, _ = _dispatcher(tmp_path, [_say({"decision": "allow"}), _say({"decision": "defer"})])
    assert (await d.fire("onStop", _payload())).decision == "defer"


# ---------------------------------------------------------------- block


async def test_block_continues_turn_and_returns_reason(world: World):
    gate = Gate(world.tmp)
    gate.reply(1, {"decision": "block", "reason": "write the outcome file"})
    cron = {"id": "c1", "type": "cron", "status": "scheduled", "description": "*/5 * * * *"}
    scenario = turn(
        [{"text": "first leg"}],
        on_block=[{"sleep": 1.0}, {"text": "second leg"}],
        stop={"session_crons": [cron]},
    )
    m, sid = await start(world, world.cfg(scenario, hooks={"onStop": [gate.action()]}))
    router = m.router(sid)
    record = await router.submit_turn("go")
    await world.until(lambda: of_type(world, sid, "turn_continued"))
    # Held open: the turn is still the running one and the session stays busy.
    assert router.session.current_turn is record and record.state == "running"
    assert m.get(sid)["state"] == "busy"
    (cont,) = of_type(world, sid, "turn_continued")
    assert cont == {
        "turn_id": record.turn_id,
        "reason": "write the outcome file",
        "stop_hook_active": False,
    }
    done = await wait_turn(m, sid)
    assert done.turn_id == record.turn_id and done.final_text == "second leg"
    # The reason reached claude: the fake writes it as the user line of the next leg.
    lines = [json.loads(x) for x in Path(router.session.transcript_path).read_text().splitlines()]
    assert {"role": "user", "content": "write the outcome file"} in [
        x.get("message") for x in lines
    ]
    first, second = Gate.calls(world, sid)
    assert first["event"] == "onStop" and first["turn_id"] == record.turn_id
    assert first["data"] == {
        "turn_id": record.turn_id,
        "stop_hook_active": False,
        "last_assistant_message": "first leg",
        "background_tasks": [],
        "session_crons": [cron],
        "transcript_path": router.session.transcript_path,
    }
    assert second["data"]["stop_hook_active"] is True
    assert second["data"]["last_assistant_message"] == "second leg"
    assert types(world, sid, "turn_continued", "turn_completed") == [
        "turn_continued",
        "turn_completed",
    ]


async def test_block_keeps_usage(world: World):
    gate = Gate(world.tmp)
    gate.reply(1, {"decision": "block", "reason": "again"})
    scenario = turn([{"text": "one two three"}], on_block=[{"text": "four five"}])
    m, sid = await start(world, world.cfg(scenario, hooks={"onStop": [gate.action()]}))
    await m.router(sid).submit_turn("go")
    done = await wait_turn(m, sid)
    assert done.usage["output_tokens"] == 5
    assert done.model_tokens["fake-claude"]["output_tokens"] == 5


async def test_exit_2_blocks(world: World, tmp_path):
    exit2 = {"type": "command", "run": "echo ' nope ' >&2; exit 2", "blocking": True}
    d, _ = _dispatcher(tmp_path, [exit2])
    o = await d.fire("onStop", _payload())
    assert (o.decision, o.reason) == ("block", "nope")

    gate = Gate(world.tmp)
    gate.exit2(1, "keep going\n")
    scenario = turn([{"text": "leg one"}], on_block=[{"text": "leg two"}])
    m, sid = await start(world, world.cfg(scenario, hooks={"onStop": [gate.action()]}))
    await m.router(sid).submit_turn("go")
    done = await wait_turn(m, sid)
    assert done.final_text == "leg two"
    (cont,) = of_type(world, sid, "turn_continued")
    assert cont["reason"] == "keep going"


async def test_deny_not_honoured_on_on_stop(world: World):
    gate = Gate(world.tmp)
    gate.reply(1, {"decision": "deny", "reason": "no"})
    m, sid = await start(
        world, world.cfg(turn([{"text": "done"}]), hooks={"onStop": [gate.action()]})
    )
    record = await m.router(sid).submit_turn("go")
    done = await wait_turn(m, sid)
    assert done.turn_id == record.turn_id and done.final_text == "done"
    assert len(Gate.calls(world, sid)) == 1
    assert not of_type(world, sid, "turn_continued")
    (res,) = [r for r in of_type(world, sid, "hook_result") if r["event"] == "onStop"]
    assert res["decision"] == "allow" and res["invalid_decision"] == "deny"


# ---------------------------------------------------------------- defer


def _defer_cfg(world: World, scenario: dict, **sections: Any) -> Config:
    sessions = {"defer_stop_on_background_tasks": True, **sections.pop("sessions", {})}
    return world.cfg(scenario, sessions=sessions, **sections)


async def test_defer_flag_keeps_turn_busy_until_tasks_empty(world: World):
    scenario = turn(
        [
            {"text": "started tests"},
            {"background": {"after": 1.5, "text": "tests passed", "background_tasks": []}},
        ],
        stop={"background_tasks": [TASK]},
    )
    m, sid = await start(world, _defer_cfg(world, scenario))
    router = m.router(sid)
    record = await router.submit_turn("go")
    await world.until(lambda: of_type(world, sid, "turn_deferred"))
    assert of_type(world, sid, "turn_deferred") == [
        {"turn_id": record.turn_id, "background_tasks": [TASK]}
    ]
    assert router.session.current_turn is record and m.get(sid)["state"] == "busy"
    done = await wait_turn(m, sid)
    assert done.turn_id == record.turn_id and done.final_text == "tests passed"
    assert done.origin == "client"
    assert m.get(sid)["state"] == "ready"
    assert len(of_type(world, sid, "turn_completed")) == 1


async def test_hook_cannot_allow_past_builtin_defer(world: World):
    gate = Gate(world.tmp)
    gate.reply(1, {"decision": "allow"})
    gate.reply(2, {"decision": "block", "reason": "summarise the tasks"})
    scenario = turn(
        [
            {"text": "started"},
            {"background": {"after": 1.0, "text": "still going", "background_tasks": [TASK]}},
        ],
        stop={"background_tasks": [TASK]},
    )
    m, sid = await start(world, _defer_cfg(world, scenario, hooks={"onStop": [gate.action()]}))
    record = await m.router(sid).submit_turn("go")
    done = await wait_turn(m, sid)
    assert done.turn_id == record.turn_id and done.final_text == "still going"
    # 1: tasks running, hook allow -> still deferred; 2: tasks running, hook block -> block
    # wins; 3: after the block, no tasks -> allow completes.
    assert [c["data"]["background_tasks"] for c in Gate.calls(world, sid)] == [[TASK], [TASK], []]
    assert types(world, sid, "turn_deferred", "turn_continued", "turn_completed") == [
        "turn_deferred",
        "turn_continued",
        "turn_completed",
    ]


async def test_deferred_turn_hits_turn_max_seconds(world: World):
    scenario = turn([{"text": "started"}], stop={"background_tasks": [TASK]})
    cfg = _defer_cfg(
        world,
        scenario,
        sessions={"interrupt_settle_seconds": 0.3},
        limits={"turn_max_seconds": 1.5},
    )
    m, sid = await start(world, cfg)
    record = await m.router(sid).submit_turn("go")
    await world.until(lambda: of_type(world, sid, "turn_deferred"))
    done = await wait_turn(m, sid, "timeout")
    assert done.turn_id == record.turn_id
    assert not of_type(world, sid, "turn_completed")
    (failed,) = of_type(world, sid, "turn_failed")
    assert failed["record"]["state"] == "timeout"
    assert m.get(sid)["state"] == "ready"


async def test_deferred_turn_can_be_interrupted(world: World):
    scenario = turn([{"text": "started"}], stop={"background_tasks": [TASK]})
    cfg = _defer_cfg(world, scenario, sessions={"interrupt_settle_seconds": 0.3})
    m, sid = await start(world, cfg)
    router = m.router(sid)
    record = await router.submit_turn("go")
    await world.until(lambda: of_type(world, sid, "turn_deferred"))
    assert await router.interrupt() == record.turn_id
    done = await wait_turn(m, sid, "interrupted")
    assert done.turn_id == record.turn_id
    assert router.session.current_turn is None and m.get(sid)["state"] == "ready"
    assert not of_type(world, sid, "turn_completed")


# ---------------------------------------------------------------- completion bookkeeping


async def test_no_double_completion(world: World):
    gate = Gate(world.tmp)
    gate.reply(1, {"decision": "block", "reason": "once more"})
    completes = world.tmp / "complete.log"
    scenario = turn(
        [
            {"text": "a"},
            {"background": {"after": 1.0, "text": "bg finished", "background_tasks": []}},
        ],
        on_block=[{"text": "b"}],
    )
    # Stop 1 blocks; stop 2 (after the block, the fake sends no tasks) defers by the gate
    # reply; stop 3 (the background completion) allows.
    gate.reply(2, {"decision": "defer"})
    hooks = {"onStop": [gate.action()], "onTurnComplete": [counter(completes)]}
    m, sid = await start(world, world.cfg(scenario, hooks=hooks))
    record = await m.router(sid).submit_turn("go", callback_url=f"{world.url}/cb")
    done = await wait_turn(m, sid)
    assert done.turn_id == record.turn_id and done.final_text == "bg finished"
    await m.router(sid).drain(5)
    await world.until(lambda: world.callbacks)
    assert len(Gate.calls(world, sid)) == 3
    assert types(world, sid, "turn_continued", "turn_deferred", "turn_completed") == [
        "turn_continued",
        "turn_deferred",
        "turn_completed",
    ]
    assert len([x for x in completes.read_text().splitlines() if x.strip()]) == 1
    assert [c["type"] for c in world.callbacks] == ["turn_completed"]
    assert not of_type(world, sid, "turn_failed")
    assert [t.turn_id for t in m.open_turns(sid).list()] == [record.turn_id]


# ---------------------------------------------------------------- stops that are not gated


async def test_stale_or_foreign_stop_fires_no_on_stop(world: World):
    gate = Gate(world.tmp)
    m, sid = await start(
        world,
        world.cfg(
            turn([{"sleep": 30}]),
            hooks={"onStop": [gate.action()]},
            sessions={"defer_stop_on_background_tasks": True, "interrupt_settle_seconds": 0.3},
        ),
    )
    router = m.router(sid)
    session = router.session
    record = await router.submit_turn("go")
    claude_sid = router.info.claude_session_id
    assert session.stop_target({"session_id": claude_sid}, record.turn_id) is record
    assert session.stop_target({"session_id": claude_sid}, None) is record

    foreign = {"session_id": "someone-else", "background_tasks": [TASK]}
    assert session.stop_target(foreign, record.turn_id) is None
    assert await router.handle_claude_hook("Stop", foreign) == {
        "stdout": "",
        "stderr": "",
        "exit_code": 0,
    }

    assert session.stop_target({"session_id": claude_sid}, "turn-0older") is None
    # A stale Stop: the transcript's last marker names an older turn (written after the
    # prompt line that carries this turn's marker).
    transcript = Path(session.transcript_path)
    await world.until(lambda: transcript.exists() and record.turn_id in transcript.read_text())
    with open(session.transcript_path, "a", encoding="utf-8") as f:
        line = {"type": "user", "message": {"role": "user", "content": "x <!-- ccw turn: t0 -->"}}
        f.write(json.dumps(line) + "\n")
    stale = {"session_id": claude_sid, "background_tasks": [TASK]}
    assert (await router.handle_claude_hook("Stop", stale))["stdout"] == ""

    assert Gate.calls(world, sid) == []
    assert session.current_turn is record and record.state == "running"
    assert not of_type(world, sid, "turn_deferred") and not of_type(world, sid, "turn_completed")
    await router.interrupt()
    assert Gate.calls(world, sid) == []


async def test_agent_origin_stop_unchanged(world: World):
    gate = Gate(world.tmp)
    scenario = turn(
        [
            {"text": "turn text"},
            {"background": {"after": 1.0, "text": "monitor fired", "background_tasks": [TASK]}},
        ]
    )
    cfg = _defer_cfg(world, scenario, hooks={"onStop": [gate.action()]})
    m, sid = await start(world, cfg)
    record = await m.router(sid).submit_turn("go")
    await world.until(lambda: len(of_type(world, sid, "turn_completed")) == 2)
    first, second = m.open_turns(sid).list()
    assert first.turn_id == record.turn_id and first.state == "completed"
    assert second.origin == "agent" and second.state == "completed"
    assert second.final_text == "monitor fired"
    # Only the client turn's Stop was gated; the background completion fired no onStop.
    assert len(Gate.calls(world, sid)) == 1
    assert not of_type(world, sid, "turn_deferred")


async def test_turn_ended_while_gate_ran_is_neither_continued_nor_completed(world: World):
    slow_block = {
        "type": "command",
        "run": 'sleep 1.5; printf \'%s\' \'{"decision": "block", "reason": "more"}\'',
        "blocking": True,
    }
    cfg = world.cfg(
        turn([{"text": "done"}]),
        hooks={"onStop": [slow_block]},
        sessions={"interrupt_settle_seconds": 0.3},
    )
    m, sid = await start(world, cfg)
    router = m.router(sid)
    record = await router.submit_turn("go")
    await world.until(lambda: any(e["type"] == "assistant_text" for e in world.events(sid)))
    await router.interrupt()
    await world.until(
        lambda: [r for r in of_type(world, sid, "hook_result") if r["event"] == "onStop"]
    )
    await router.drain(2)
    assert record.state == "interrupted"
    assert not of_type(world, sid, "turn_continued") and not of_type(world, sid, "turn_completed")
    assert [t.turn_id for t in m.open_turns(sid).list()] == [record.turn_id]
