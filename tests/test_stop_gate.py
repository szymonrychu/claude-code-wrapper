"""K1 stop gate: the onStop hook event (allow, block, defer) and the background-task defer."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from conftest import World, jsonl, wait_turn
from test_router import assistant

from claude_code_wrapper.bootstrap.settings import RelayTimeouts, relay_timeouts
from claude_code_wrapper.config import HOOK_EVENTS, Config, HooksConfig
from claude_code_wrapper.events import EVENT_TYPES
from claude_code_wrapper.hooks import EVENT_DECISIONS, HookDispatcher, build_payload
from claude_code_wrapper.manager import CreateSessionRequest
from claude_code_wrapper.redact import Redactor
from claude_code_wrapper.session import TurnRejected

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
        [{"text": "first leg", "message_id": "msg_first"}],
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
        "assistant_texts": [{"message_id": "msg_first", "text": "first leg"}],
        "assistant_texts_truncated": False,
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
        {
            "turn_id": record.turn_id,
            "reason": "background_tasks",
            "background_tasks": [TASK],
            "commands": [],
        }
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


async def test_stop_ends_a_turn_whose_prompt_is_a_queued_command_attachment(world: World):
    """An absorbed prompt (Claude took the paste mid-turn) is recorded as an attachment line."""
    gate = Gate(world.tmp)
    m, sid = await start(world, world.cfg(turn([{"sleep": 30}]), hooks={"onStop": [gate.action()]}))
    router = m.router(sid)
    session = router.session
    record = await router.submit_turn("go")
    transcript = Path(session.transcript_path)
    await world.until(lambda: transcript.exists() and record.turn_id in transcript.read_text())
    # The previous turn's marker is the last one seen; this turn's marker only arrives inside
    # the queued_command attachment.
    older = {"type": "user", "message": {"role": "user", "content": "x <!-- ccw turn: t0 -->"}}
    absorbed = {
        "type": "attachment",
        "attachment": {
            "type": "queued_command",
            "prompt": f"go\n\n<!-- ccw turn: {record.turn_id} -->",
            "commandMode": "prompt",
        },
    }
    with open(session.transcript_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(older) + "\n" + json.dumps(absorbed) + "\n")
    stop = {"session_id": router.info.claude_session_id}
    assert (await router.handle_claude_hook("Stop", stop))["exit_code"] == 0

    assert len(Gate.calls(world, sid)) == 1
    assert record.state == "completed" and session.current_turn is None
    assert session.state == "ready"
    assert len(of_type(world, sid, "turn_completed")) == 1


def _agent_turn(world: World, sid: str, m: Any) -> Any:
    agent = [t for t in m.open_turns(sid).list() if t.origin == "agent"]
    assert len(agent) == 1, agent
    return agent[0]


async def test_agent_origin_turn_is_adopted_and_gated(world: World):
    # Claude woke by itself on a ready session (a background completion): its first main-chain
    # record opens a running agent-origin turn, so its Stop is gated like any turn's.
    gate = Gate(world.tmp)
    scenario = turn(
        [{"text": "turn text"}, {"background": {"after": 1.0, "text": "monitor fired"}}]
    )
    cfg = world.cfg(scenario, hooks={"onStop": [gate.action()]})
    m, sid = await start(world, cfg)
    record = await m.router(sid).submit_turn("go")
    await world.until(lambda: len(of_type(world, sid, "turn_completed")) == 2)
    first, second = m.open_turns(sid).list()
    assert first.turn_id == record.turn_id and first.state == "completed"
    assert second.origin == "agent" and second.state == "completed"
    assert second.final_text == "monitor fired"
    started = of_type(world, sid, "turn_started")
    assert [(s["turn_id"], s["origin"]) for s in started] == [
        (first.turn_id, "client"),
        (second.turn_id, "agent"),
    ]
    calls = Gate.calls(world, sid)
    assert len(calls) == 2
    agent_call = calls[1]["data"]
    assert agent_call["turn_id"] == second.turn_id
    assert [t["text"] for t in agent_call["assistant_texts"]] == ["monitor fired"]
    texts = {t["text"]: t["turn_id"] for t in of_type(world, sid, "assistant_text")}
    assert texts == {"turn text": first.turn_id, "monitor fired": second.turn_id}
    assert m.get(sid)["state"] == "ready"


async def test_agent_origin_turn_block_continues_the_same_turn(world: World):
    gate = Gate(world.tmp)
    gate.reply(2, {"decision": "block", "reason": "close it"})
    scenario = turn(
        [{"text": "turn text"}, {"background": {"after": 1.0, "text": "monitor fired"}}]
    )
    cfg = world.cfg(scenario, hooks={"onStop": [gate.action()]})
    m, sid = await start(world, cfg)
    await m.router(sid).submit_turn("go")
    await world.until(lambda: len(of_type(world, sid, "turn_completed")) == 2)
    agent = _agent_turn(world, sid, m)
    calls = [c["data"] for c in Gate.calls(world, sid)]
    assert len(calls) == 3
    assert calls[1]["turn_id"] == calls[2]["turn_id"] == agent.turn_id
    assert calls[1]["stop_hook_active"] is False and calls[2]["stop_hook_active"] is True
    assert of_type(world, sid, "turn_continued") == [
        {"turn_id": agent.turn_id, "reason": "close it", "stop_hook_active": False}
    ]
    assert [s["origin"] for s in of_type(world, sid, "turn_started")] == ["client", "agent"]
    assert agent.state == "completed"


async def test_agent_origin_turn_keeps_background_defer(world: World):
    # The built-in background-task defer applies to an adopted turn as to any turn: it stays
    # running until a Stop with no background tasks.
    scenario = turn(
        [
            {"text": "turn text"},
            {"background": {"after": 1.0, "text": "monitor fired", "background_tasks": [TASK]}},
            {"background": {"after": 2.0, "text": "all done"}},
        ]
    )
    m, sid = await start(world, _defer_cfg(world, scenario))
    await m.router(sid).submit_turn("go")
    await world.until(lambda: of_type(world, sid, "turn_deferred"))
    agent = _agent_turn(world, sid, m)
    assert agent.state == "running" and m.get(sid)["state"] == "busy"
    deferred = of_type(world, sid, "turn_deferred")
    assert [(d["turn_id"], d["reason"]) for d in deferred] == [(agent.turn_id, "background_tasks")]
    await world.until(lambda: len(of_type(world, sid, "turn_completed")) == 2)
    agent = _agent_turn(world, sid, m)
    assert agent.state == "completed" and agent.final_text == "all done"


async def test_delivery_while_agent_origin_turn_runs_is_busy_handled(world: World):
    # A client turn submitted while an adopted turn runs meets a busy session: the idle lane
    # is refused, the regular lane types the text into the running agent-origin turn.
    slow = {"type": "command", "run": "sleep 2", "blocking": True}
    scenario = turn(
        [{"text": "turn text"}, {"background": {"after": 0.5, "text": "monitor fired"}}]
    )
    cfg = world.cfg(scenario, hooks={"onStop": [slow]})
    m, sid = await start(world, cfg)
    router = m.router(sid)
    await router.submit_turn("go")
    await world.until(lambda: len(of_type(world, sid, "turn_started")) == 2)
    agent = _agent_turn(world, sid, m)
    assert agent.state == "running" and m.get(sid)["state"] == "busy"
    with pytest.raises(TurnRejected) as exc:
        await router.submit_turn("hello")
    assert exc.value.reason == "busy"
    delivery = await router.deliver("hello", lane="regular")
    assert delivery.message is not None and delivery.message.turn_id == agent.turn_id
    queued = of_type(world, sid, "prompt_queued")
    assert [q["turn_id"] for q in queued] == [agent.turn_id]
    await world.until(lambda: m.get(sid)["state"] == "ready")
    assert _agent_turn(world, sid, m).state == "completed"


def _append(m: Any, sid: str, *lines: str) -> None:
    """Lines claude wrote to the main transcript (a record the tailer reads late, a wake)."""
    with open(m.router(sid).session.transcript_path, "a") as f:
        f.writelines(lines)


async def _settled(world: World, m: Any, sid: str, text: str) -> None:
    """The tailer read the line carrying `text` (it emitted its assistant_text)."""
    await world.until(
        lambda: [t for t in of_type(world, sid, "assistant_text") if t["text"] == text]
    )
    await m.router(sid).drain(0.5)


async def test_late_record_after_a_stopless_end_is_not_adopted(world: World):
    # The turn ends without a Stop and without an interrupt line (second-ESC path): a main-chain
    # record read afterwards may still belong to it, so it opens no phantom agent turn.
    scenario = {"turns": [{"match": "wedge", "steps": [{"print": "working...\n"}]}]}
    cfg = world.cfg(scenario, sessions={"interrupt_settle_seconds": 0.3})
    m, sid = await start(world, cfg)
    router = m.router(sid)
    record = await router.submit_turn("wedge")
    await world.until(lambda: router.session.ring.contains("working..."))
    await router.interrupt()
    assert record.state == "interrupted" and m.get(sid)["state"] == "ready"
    _append(m, sid, assistant("late words", mid="late1"))
    await _settled(world, m, sid, "late words")
    assert [s["origin"] for s in of_type(world, sid, "turn_started")] == ["client"]
    assert [t.origin for t in m.open_turns(sid).list()] == ["client"]
    assert m.get(sid)["state"] == "ready"
    [late] = [t for t in of_type(world, sid, "assistant_text") if t["text"] == "late words"]
    assert late["turn_id"] is None


async def test_record_after_an_interrupt_line_is_adopted(world: World):
    # The interrupt line closes claude's turn in transcript order: a record after it is a wake.
    cfg = world.cfg(turn([{"text": "part"}, {"sleep": 30}]))
    m, sid = await start(world, cfg)
    router = m.router(sid)
    record = await router.submit_turn("go")
    await world.until(lambda: of_type(world, sid, "assistant_text"))
    await router.interrupt()
    assert record.state == "interrupted"
    _append(m, sid, assistant("woke up", mid="wake1"))
    await world.until(lambda: len(of_type(world, sid, "turn_started")) == 2)
    assert _agent_turn(world, sid, m).state == "running"


async def test_sidechain_and_subagent_records_never_adopt(world: World):
    m, sid = await start(world, world.cfg(turn([{"text": "turn text"}])))
    await m.router(sid).submit_turn("go")
    await world.until(
        lambda: m.get(sid)["state"] == "ready" and of_type(world, sid, "turn_completed")
    )
    side = json.loads(assistant("side words", mid="side1"))
    side["isSidechain"] = True
    sub = json.loads(assistant("sub words", mid="sub1"))
    sub["agentId"] = "a1"
    _append(m, sid, json.dumps(side) + "\n", json.dumps(sub) + "\n")
    await _settled(world, m, sid, "sub words")
    assert [s["origin"] for s in of_type(world, sid, "turn_started")] == ["client"]
    assert m.get(sid)["state"] == "ready"


async def test_interrupt_of_an_adopted_turn_ends_it_and_returns_to_ready(world: World):
    cfg = world.cfg(turn([{"text": "turn text"}]), sessions={"interrupt_settle_seconds": 0.3})
    m, sid = await start(world, cfg)
    router = m.router(sid)
    await router.submit_turn("go")
    await world.until(
        lambda: m.get(sid)["state"] == "ready" and of_type(world, sid, "turn_completed")
    )
    _append(m, sid, assistant("woke up", mid="wake1"))
    await world.until(lambda: m.get(sid)["state"] == "busy")
    agent = _agent_turn(world, sid, m)
    assert await router.interrupt() == agent.turn_id
    assert _agent_turn(world, sid, m).state == "interrupted"
    assert m.get(sid)["state"] == "ready"


async def test_probe_on_a_ready_session_is_not_adopted(world: World):
    # A probe claude answers as a turn of its own completes as before: an agent record at its
    # Stop, no turn_started, no onStop, never busy.
    gate = Gate(world.tmp)
    cfg = world.cfg({}, hooks={"onStop": [gate.action()]})
    m, sid = await start(world, cfg)
    probe = await m.router(sid).session.probe()
    await world.until(lambda: probe.state == "answered")
    await world.until(lambda: [t for t in m.open_turns(sid).list() if t.origin == "agent"])
    assert _agent_turn(world, sid, m).state == "completed"
    assert not of_type(world, sid, "turn_started")
    assert Gate.calls(world, sid) == []
    assert "busy" not in [s["state"] for s in of_type(world, sid, "session_state")]


async def test_adopted_turn_is_not_continued_after_a_crash(world: World):
    # claude dies while an adopted turn runs: nobody asked for it, so it fails and no restart
    # prompt is pasted for it.
    cfg = world.cfg(
        turn([{"text": "turn text"}]),
        sessions={"restart_continue_prompt": "Resume: continue the task"},
    )
    m, sid = await start(world, cfg)
    router = m.router(sid)
    await router.submit_turn("go")
    await world.until(
        lambda: m.get(sid)["state"] == "ready" and of_type(world, sid, "turn_completed")
    )
    _append(m, sid, assistant("woke up", mid="wake1"))
    await world.until(lambda: m.get(sid)["state"] == "busy")
    await router.session.pty.write(b"\x03")  # ctrl+c at idle: the fake exits
    await world.until(lambda: _agent_turn(world, sid, m).state != "running")
    agent = _agent_turn(world, sid, m)
    assert (agent.state, agent.error) == ("failed", "claude_exited")
    await world.until(lambda: m.get(sid)["state"] == "ready")
    await router.drain(0.5)
    assert not of_type(world, sid, "turn_resumed")
    inputs = world.session_dir(sid) / "config" / "fake-inputs.jsonl"
    assert not [i for i in jsonl(inputs) if i["text"].startswith("Resume")]


async def test_adopted_turn_left_running_by_a_shutdown_is_not_continued(world: World):
    cfg = world.cfg(
        turn([{"text": "turn text"}]),
        sessions={"restart_continue_prompt": "Resume: continue the task", "resume_on_start": True},
    )
    m1, sid = await start(world, cfg)
    await m1.router(sid).submit_turn("go")
    await world.until(
        lambda: m1.get(sid)["state"] == "ready" and of_type(world, sid, "turn_completed")
    )
    _append(m1, sid, assistant("woke up", mid="wake1"))
    await world.until(lambda: m1.get(sid)["state"] == "busy")
    tid = _agent_turn(world, sid, m1).turn_id
    await m1.shutdown()
    m2 = world.manager(cfg)
    await m2.load_persisted()
    await world.until(lambda: m2.open_turns(sid).get(tid).state != "running")
    done = m2.open_turns(sid).get(tid)
    assert (done.state, done.error) == ("failed", "service_shutdown")
    assert not of_type(world, sid, "turn_resumed")
    await world.until(lambda: m2.get(sid)["state"] == "ready")
    await m2.shutdown()


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
