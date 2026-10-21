"""SessionRouter: Claude hooks, transcript lines and session callbacks become events and hooks."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from claude_code_wrapper.config import Config
from claude_code_wrapper.events import EventLog
from claude_code_wrapper.hooks import HookDispatcher
from claude_code_wrapper.redact import Redactor
from claude_code_wrapper.router import QUESTION_DENY_REASON, SessionInfo, SessionRouter, TurnVetoed
from claude_code_wrapper.session import TurnRejected
from claude_code_wrapper.turns import Probe, TurnRecord, now_iso

SID = "ses_0123456789ab"
CLAUDE_SID = "claude-uuid"
SECRET = "topsecretvalue123"


async def wait_until(pred, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


class StubSession:
    """Just the Session surface the router uses, recording every call."""

    def __init__(self, workdir: Path, transcript: Path) -> None:
        self.spec = SimpleNamespace(resume=False)
        self.transcript_path = str(transcript)
        self.turn_store = None
        self.turn: TurnRecord | None = None
        self.calls: list[tuple] = []
        self.complete_result: TurnRecord | None = None
        self.probes: dict[str, Probe] = {}
        self.interrupt_result: str | None = None
        self.reject: str | None = None
        self.state = "ready"
        self.submitted_turn_ids: list[str | None] = []
        self.command_active = False

    @property
    def current_turn(self):
        return self.turn

    def snapshot(self):
        return {"state": self.state}

    def note_activity(self):
        self.calls.append(("note_activity",))

    def record_tool_call(self):
        self.calls.append(("record_tool_call",))

    def on_interrupted(self):
        self.calls.append(("on_interrupted",))

    def complete(self, payload, *, usage=None, model_tokens=None, marker_turn_id=None):
        self.calls.append(("complete", payload, usage, model_tokens, marker_turn_id))
        return self.complete_result

    def check_submittable(self):
        if self.reject:
            raise TurnRejected(self.reject)

    async def submit(
        self,
        text,
        *,
        origin="client",
        callback_url=None,
        turn_id=None,
        annotations=None,
        metadata=None,
    ):
        if self.reject:
            raise TurnRejected(self.reject)
        self.calls.append(("submit", text, origin, callback_url))
        self.submitted_turn_ids.append(turn_id)
        self.turn = TurnRecord(
            turn_id=turn_id or "turn-1",
            session_id=SID,
            origin=origin,
            text=text,
            callback_url=callback_url,
            started_at=now_iso(),
        )
        return self.turn

    async def interrupt(self):
        self.calls.append(("interrupt",))
        return self.interrupt_result

    def probe_delivered(self, probe_id):
        self.calls.append(("probe_delivered", probe_id))
        probe = self.probes.get(probe_id)
        if probe:
            probe.state = "delivered"
        return probe

    def probe_answered(self, answer=""):
        self.calls.append(("probe_answered",))
        out = [p for p in self.probes.values() if p.state == "delivered"]
        for p in out:
            p.state = "answered"
            p.answer = answer[:2000]
        return out


class Harness:
    def __init__(self, tmp_path: Path, hooks: dict | None = None, sessions: dict | None = None):
        self.tmp = tmp_path
        self.workdir = tmp_path / "workdir"
        self.workdir.mkdir()
        self.transcript = tmp_path / "t" / "main.jsonl"
        self.transcript.parent.mkdir()
        self.posts: list[httpx.Request] = []
        self.hook_posts: list[dict] = []
        self.cfg = Config.model_validate(
            {
                "server": {"callback_secret": "cb-secret-123456"},
                "sessions": sessions or {},
                "hooks": hooks or {},
            }
        )
        self.redactor = Redactor([SECRET])
        self.log = EventLog(tmp_path / "events.jsonl", SID)
        self.dispatcher = HookDispatcher(
            self.cfg.hooks,
            self.redactor,
            base_env={"PATH": os.environ["PATH"]},
            default_cwd=self.workdir,
            transport=httpx.MockTransport(self._handler),
        )
        self.router = SessionRouter(
            None,
            self.log,
            self.dispatcher,
            self.redactor,
            self.cfg,
            session_info=SessionInfo(SID, CLAUDE_SID, str(self.workdir), "http://session.cb/x"),
        )
        self.session = StubSession(self.workdir, self.transcript)
        self.router.bind(self.session)  # type: ignore[arg-type]

    def _handler(self, req: httpx.Request) -> httpx.Response:
        self.posts.append(req)
        body = json.loads(req.content)
        if req.url.host == "hook.test":
            self.hook_posts.append(body)
            return httpx.Response(200, json=self.reply)
        return httpx.Response(200)

    reply: dict = {}

    def events(self, type: str | None = None):
        evs = self.log.last(1000)
        return [e for e in evs if type is None or e.type == type]

    def calls(self, name: str):
        return [c for c in self.session.calls if c[0] == name]


def webhook(**kw):
    return {"type": "webhook", "url": "http://hook.test/h", "blocking": True, "retries": 0, **kw}


@pytest.fixture
async def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    await harness.router.close()


def tx_line(**msg) -> str:
    return json.dumps(msg) + "\n"


def assistant(text: str, mid: str = "m1", usage: dict | None = None) -> str:
    return tx_line(
        type="assistant",
        message={
            "id": mid,
            "model": "claude-x",
            "content": [{"type": "text", "text": text}],
            "usage": usage or {"input_tokens": 3, "output_tokens": 5},
        },
    )


def user(text: str) -> str:
    return tx_line(type="user", message={"role": "user", "content": text})


# ---------------------------------------------------------------- relay: pre tool use
async def test_router_exposes_parts(h):
    assert h.router.session is h.session and h.router.event_log is h.log
    r = await h.router.handle_claude_hook("UserPromptSubmit", {"prompt": "x"})
    assert r == {"stdout": "", "stderr": "", "exit_code": 0}
    assert h.calls("note_activity")


async def test_transcript_path_switch(h, tmp_path):
    await h.router.handle_claude_hook("UserPromptSubmit", {})
    assert h.router.tailer.path == h.transcript
    other = tmp_path / "other.jsonl"
    await h.router.handle_claude_hook("UserPromptSubmit", {"transcript_path": str(other)})
    assert h.router.tailer.path == other and h.session.transcript_path == str(other)


async def test_pretooluse_allow_emits_tool_call(h):
    r = await h.router.handle_claude_hook(
        "PreToolUse",
        {"tool_use_id": "tu1", "tool_name": "Bash", "tool_input": {"command": f"echo {SECRET}"}},
    )
    assert r["stdout"] == "" and r["exit_code"] == 0
    (ev,) = h.events("tool_call")
    assert ev.data["tool_use_id"] == "tu1" and ev.data["tool_name"] == "Bash"
    assert SECRET not in json.dumps(ev.data)


async def test_pretooluse_deny_returns_deny_json_and_tool_denied(tmp_path):
    h = Harness(tmp_path, hooks={"onAgentToolCall": [webhook()]})
    h.reply = {"decision": "deny", "reason": "no bash"}
    r = await h.router.handle_claude_hook(
        "PreToolUse", {"tool_use_id": "tu1", "tool_name": "Bash", "tool_input": {}}
    )
    out = json.loads(r["stdout"])["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and out["permissionDecisionReason"] == "no bash"
    (ev,) = h.events("tool_denied")
    assert ev.data == {
        "tool_use_id": "tu1",
        "tool_name": "Bash",
        "reason": "no bash",
        "agent_id": None,
        "agent_type": None,
    }
    assert h.hook_posts[0]["data"]["tool_name"] == "Bash"
    await h.router.close()


async def test_additional_context_returned(tmp_path):
    h = Harness(tmp_path, hooks={"onAgentToolCall": [webhook()]})
    h.reply = {"additional_context": "be careful"}
    r = await h.router.handle_claude_hook(
        "PreToolUse", {"tool_use_id": "t", "tool_name": "Read", "tool_input": {}}
    )
    assert json.loads(r["stdout"]) == {
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "be careful"}
    }
    await h.router.close()


async def test_ask_user_question_forwarded(h):
    h.session.turn = TurnRecord(
        turn_id="turn-1", session_id=SID, started_at=now_iso(), callbackUrl="http://turn.cb/q"
    )
    questions = {"questions": [{"question": "A or B?"}]}
    r = await h.router.handle_claude_hook(
        "PreToolUse", {"tool_use_id": "q1", "tool_name": "AskUserQuestion", "tool_input": questions}
    )
    out = json.loads(r["stdout"])["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == QUESTION_DENY_REASON
    (ev,) = h.events("question")
    assert ev.data == {"tool_use_id": "q1", "questions": questions}
    assert not h.events("tool_call")
    await h.router.drain()
    (req,) = h.posts
    assert str(req.url) == "http://turn.cb/q"
    assert json.loads(req.content)["type"] == "question"
    assert req.headers["x-ccw-signature"].startswith("sha256=")


async def test_question_posted_to_callback_url(h):
    await h.router.handle_claude_hook(
        "PreToolUse", {"tool_use_id": "q", "tool_name": "AskUserQuestion", "tool_input": {}}
    )
    await h.router.drain()
    assert [str(r.url) for r in h.posts] == ["http://session.cb/x"]


async def test_ask_user_question_no_callback_no_post(tmp_path):
    h = Harness(tmp_path)
    h.router.info.callback_url = None
    await h.router.handle_claude_hook(
        "PreToolUse", {"tool_use_id": "q", "tool_name": "AskUserQuestion", "tool_input": {}}
    )
    await h.router.drain()
    assert h.posts == [] and h.events("question")
    await h.router.close()


async def test_ask_user_question_not_forwarded_when_allow(tmp_path):
    h = Harness(tmp_path, sessions={"ask_user_question": "allow"})
    r = await h.router.handle_claude_hook(
        "PreToolUse", {"tool_use_id": "q", "tool_name": "AskUserQuestion", "tool_input": {}}
    )
    assert r["stdout"] == "" and h.events("tool_call") and not h.events("question")
    await h.router.close()


# ---------------------------------------------------------------- relay: post tool use
async def test_posttooluse_emits_tool_result_redacted(tmp_path):
    h = Harness(tmp_path, hooks={"onToolResult": [webhook()]})
    h.reply = {"additional_context": "more"}
    r = await h.router.handle_claude_hook(
        "PostToolUse",
        {"tool_use_id": "t1", "tool_name": "Bash", "tool_response": "x" * 5000 + SECRET},
    )
    (ev,) = h.events("tool_result")
    assert ev.data["is_error"] is False and ev.data["tool_name"] == "Bash"
    assert len(ev.data["response"]) < 4100 and SECRET not in ev.data["response"]
    assert h.calls("record_tool_call")
    assert json.loads(r["stdout"])["hookSpecificOutput"] == {
        "hookEventName": "PostToolUse",
        "additionalContext": "more",
    }
    assert h.hook_posts[0]["event"] == "onToolResult"
    await h.router.close()


async def test_post_tool_use_failure_is_error(h):
    await h.router.handle_claude_hook(
        "PostToolUseFailure", {"tool_use_id": "t", "tool_name": "Bash", "error": "boom"}
    )
    (ev,) = h.events("tool_result")
    assert ev.data["is_error"] is True and ev.data["response"] == "boom"


# ---------------------------------------------------------------- relay: stop
def completed_record(**kw) -> TurnRecord:
    base = dict(turn_id="turn-1", session_id=SID, state="completed", started_at=now_iso())
    return TurnRecord(**{**base, **kw})


async def test_stop_flushes_tailer_before_marker(h):
    h.transcript.write_text(
        user("hi\n\n<!-- ccw turn: turn-1 -->") + assistant("done", usage={"input_tokens": 7})
    )
    h.session.complete_result = completed_record()
    r = await h.router.handle_claude_hook("Stop", {"session_id": CLAUDE_SID})
    assert r == {"stdout": "", "stderr": "", "exit_code": 0}
    (call,) = h.calls("complete")
    _, payload, usage, model_tokens, marker = call
    assert marker == "turn-1"
    assert usage["input_tokens"] == 7 and model_tokens["claude-x"]["input_tokens"] == 7
    assert h.events("turn_completed")


async def test_usage_reset_after_stop(h):
    h.transcript.write_text(assistant("one", usage={"input_tokens": 7}))
    await h.router.handle_claude_hook("Stop", {"session_id": CLAUDE_SID})
    await h.router.handle_claude_hook("Stop", {"session_id": CLAUDE_SID})
    first, second = h.calls("complete")
    assert first[2]["input_tokens"] == 7
    assert not second[2] and not second[3]


async def test_stop_none_record_emits_nothing(h):
    h.session.complete_result = None
    await h.router.handle_claude_hook("Stop", {})
    assert not h.events("turn_completed") and h.posts == []


async def test_stop_completes_turn_and_fires_on_turn_complete(tmp_path):
    h = Harness(tmp_path, hooks={"onTurnComplete": [webhook()]})
    h.session.complete_result = completed_record(callbackUrl="http://turn.cb/done")
    await h.router.handle_claude_hook("Stop", {})
    assert h.hook_posts[0]["event"] == "onTurnComplete"
    assert h.hook_posts[0]["data"]["state"] == "completed"
    await h.router.drain()
    (req,) = [r for r in h.posts if r.url.host == "turn.cb"]
    body = json.loads(req.content)
    assert body["type"] == "turn_completed" and body["data"]["record"]["callbackUrl"]
    assert req.headers["x-ccw-signature"].startswith("sha256=")
    await h.router.close()


async def test_stop_callback_falls_back_to_session_url(h):
    h.session.complete_result = completed_record()
    await h.router.handle_claude_hook("Stop", {})
    await h.router.drain()
    assert [str(r.url) for r in h.posts] == ["http://session.cb/x"]


@pytest.mark.parametrize("state", ["failed", "interrupted", "timeout"])
async def test_on_turn_complete_fires_for_every_terminal_state(tmp_path, state):
    h = Harness(tmp_path, hooks={"onTurnComplete": [webhook()]})
    h.router.on_turn_end(completed_record(state=state, callbackUrl="http://turn.cb/d"))
    await h.router.drain()
    (ev,) = h.events("turn_failed")
    assert ev.data["record"]["state"] == state
    assert h.hook_posts[0]["data"]["state"] == state
    assert [str(r.url) for r in h.posts if r.url.host == "turn.cb"] == ["http://turn.cb/d"]
    await h.router.close()


async def test_on_turn_complete_envelope_carries_the_finished_turn_id(tmp_path):
    # The turn is no longer current once it finished: the envelope names it from its record.
    h = Harness(tmp_path, hooks={"onTurnComplete": [webhook()]})
    h.session.turn = None
    h.session.complete_result = completed_record(turn_id="turn-done")
    await h.router.handle_claude_hook("Stop", {})
    h.router.on_turn_end(completed_record(turn_id="turn-failed", state="failed"))
    await h.router.drain()
    assert [(p["turn_id"], p["data"]["turn_id"]) for p in h.hook_posts] == [
        ("turn-done", "turn-done"),
        ("turn-failed", "turn-failed"),
    ]
    await h.router.close()


async def test_on_turn_end_completed_does_nothing(h):
    h.router.on_turn_end(completed_record())
    await h.router.drain()
    assert not h.events("turn_failed") and h.posts == []


# ---------------------------------------------------------------- relay: subagents, errors
async def test_subagent_events(tmp_path):
    h = Harness(tmp_path, hooks={"onSubagentStart": [webhook()], "onSubagentStop": [webhook()]})
    await h.router.handle_claude_hook("SubagentStart", {"agent_id": "a1", "agent_type": "x"})
    await h.router.handle_claude_hook("SubagentStop", {"agent_id": "a1", "agent_type": "x"})
    assert h.events("subagent_started")[0].data["agent_id"] == "a1"
    assert h.events("subagent_stopped")
    assert [p["event"] for p in h.hook_posts] == ["onSubagentStart", "onSubagentStop"]
    await h.router.close()


async def test_exception_in_handler_returns_allow(tmp_path):
    h = Harness(tmp_path, hooks={"onError": [webhook()]})

    def boom(*a, **k):
        raise RuntimeError("kaput")

    h.session.record_tool_call = boom  # type: ignore[method-assign]
    r = await h.router.handle_claude_hook(
        "PostToolUse", {"tool_use_id": "t", "tool_name": "Bash", "tool_response": "ok"}
    )
    assert r == {"stdout": "", "stderr": "", "exit_code": 0}
    await h.router.drain(5)
    (ev,) = h.events("error")
    assert ev.data["kind"] == "hook_relay" and "kaput" in ev.data["message"]
    assert h.hook_posts[0]["event"] == "onError"
    await h.router.close()


# ---------------------------------------------------------------- transcript events
async def test_transcript_text_thinking_usage_marker_probe(tmp_path):
    h = Harness(tmp_path, hooks={"onAgentResponse": [webhook()]})
    h.session.probes["probe-1"] = Probe(probe_id="probe-1", text="ping", created_at=now_iso())
    h.router.ensure_tailing()
    h.transcript.write_text(
        user("go\n\n<!-- ccw turn: turn-9 -->")
        + user("ping\n\n<!-- ccw probe: probe-1 -->")
        + tx_line(
            type="assistant",
            message={
                "id": "m2",
                "model": "claude-x",
                "content": [
                    {"type": "thinking", "thinking": "hmm"},
                    {"type": "text", "text": f"CCW-ALIVE {SECRET}"},
                ],
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        )
        + user("[Request interrupted by user]")
    )
    await wait_until(lambda: h.calls("on_interrupted"))
    assert h.events("thinking")[0].data["text"] == "hmm"
    (txt,) = h.events("assistant_text")
    assert SECRET not in txt.data["text"] and txt.data["message_id"] == "m2"
    assert txt.data["model"] == "claude-x" and "agent_id" in txt.data
    assert h.calls("probe_delivered") == [("probe_delivered", "probe-1")]
    assert h.calls("probe_answered")
    assert h.events("probe")[0].data["state"] == "answered"
    assert h.hook_posts[0]["event"] == "onAgentResponse"
    assert h.router._last_marker == "turn-9"
    h.session.complete_result = None
    await h.router.handle_claude_hook("Stop", {})
    assert h.calls("complete")[0][2]["output_tokens"] == 2
    await h.router.close()


async def test_alive_without_delivered_probe_emits_no_probe_event(h):
    h.router.ensure_tailing()
    h.transcript.write_text(assistant("CCW-ALIVE"))
    await wait_until(lambda: h.events("assistant_text"))
    assert not h.events("probe")


async def test_subagent_text_has_agent_id_and_no_response_hook(tmp_path):
    h = Harness(tmp_path, hooks={"onAgentResponse": [webhook()]})
    h.router.ensure_tailing()
    sub = h.transcript.parent / "main" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-abc.jsonl").write_text(assistant("sub says hi", mid="ms"))
    await h.router._watcher.flush()
    (ev,) = h.events("assistant_text")
    assert ev.data["agent_id"] == "abc"
    assert h.hook_posts == []
    await h.router.close()


async def test_resumed_history_is_not_replayed(tmp_path):
    h = Harness(tmp_path)
    h.session.spec.resume = True
    h.transcript.write_text(assistant("old"))
    h.router.ensure_tailing()
    await asyncio.sleep(0.1)
    with h.transcript.open("a") as f:
        f.write(assistant("new", mid="m9"))
    await wait_until(lambda: h.events("assistant_text"))
    assert [e.data["text"] for e in h.events("assistant_text")] == ["new"]
    await h.router.close()


async def test_resumed_subagent_history_is_not_replayed(tmp_path):
    h = Harness(tmp_path)
    h.session.spec.resume = True
    h.transcript.write_text(assistant("old"))
    sub = h.transcript.parent / "main" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-abc.jsonl").write_text(assistant("old sub", mid="ms"))
    h.router.ensure_tailing()
    await asyncio.sleep(0.3)
    with (sub / "agent-abc.jsonl").open("a") as f:
        f.write(assistant("new sub", mid="ms2"))
    await wait_until(lambda: h.events("assistant_text"))
    assert [e.data["text"] for e in h.events("assistant_text")] == ["new sub"]
    await h.router.close()


# ---------------------------------------------------------------- turns
async def test_submit_turn_writes_files_and_emits(h):
    rec = await h.router.submit_turn(
        f"hello {SECRET}", files={"a/b.txt": "x"}, callback_url="http://c", origin="client"
    )
    assert rec.turn_id.startswith("turn-") and h.session.submitted_turn_ids == [rec.turn_id]
    assert (h.workdir / "a" / "b.txt").read_text() == "x"
    assert h.calls("submit") == [("submit", f"hello {SECRET}", "client", "http://c")]
    (ev,) = h.events("turn_started")
    assert ev.data["turn_id"] == rec.turn_id and SECRET not in ev.data["text"]
    assert h.router.tailer is not None


async def test_on_turn_veto_rejects_submit(tmp_path):
    h = Harness(tmp_path, hooks={"onTurn": [webhook()]})
    h.reply = {"decision": "deny", "reason": "not now"}
    with pytest.raises(TurnVetoed) as ei:
        await h.router.submit_turn("hi", files={"f.txt": "x"})
    assert ei.value.reason == "not now"
    assert h.events("turn_rejected")[0].data == {"reason": "not now"}
    assert not h.calls("submit") and not (h.workdir / "f.txt").exists()
    assert h.hook_posts[0]["data"] == {
        "text": "hi",
        "files": ["f.txt"],
        "origin": "client",
        "metadata": {},
    }
    await h.router.close()


async def test_on_turn_envelope_carries_the_new_turn_id(tmp_path):
    # onTurn fires before the turn exists: the router allocates the id and the turn uses it.
    h = Harness(tmp_path, hooks={"onTurn": [webhook()]})
    rec = await h.router.submit_turn("hi")
    (post,) = h.hook_posts
    assert post["event"] == "onTurn" and post["turn_id"].startswith("turn-")
    assert rec.turn_id == post["turn_id"] and h.session.submitted_turn_ids == [rec.turn_id]
    await h.router.close()


async def test_submit_turn_busy_propagates(h):
    h.session.reject = "busy"
    with pytest.raises(TurnRejected):
        await h.router.submit_turn("hi")
    assert not h.events("turn_started")


async def test_interrupt_fires_on_interrupt(tmp_path):
    h = Harness(tmp_path, hooks={"onInterrupt": [webhook()]})
    h.session.interrupt_result = "turn-1"
    assert await h.router.interrupt() == "turn-1"
    assert h.events("interrupted")[0].data == {"turn_id": "turn-1"}
    assert h.hook_posts[0]["event"] == "onInterrupt"
    assert h.hook_posts[0]["turn_id"] == "turn-1"  # the interrupted turn, not the current one
    h.session.interrupt_result = None
    assert await h.router.interrupt() is None
    assert len(h.events("interrupted")) == 1
    await h.router.close()


# ---------------------------------------------------------------- listener
async def test_listener_callbacks(tmp_path):
    h = Harness(tmp_path, hooks={"onError": [webhook()]})
    r = h.router
    r.on_state("busy")
    r.on_stall("turn-1")
    r.on_fatal(f"auth {SECRET}", "tail")
    r.on_restart(2, True)
    r.on_exit(137)
    await r.drain()
    assert h.events("session_state")[0].data == {"state": "busy"}
    assert h.events("stall_suspected")[0].data["turn_id"] == "turn-1"
    kinds = [e.data["kind"] for e in h.events("error")]
    assert kinds == ["fatal", "exited"]
    assert SECRET not in json.dumps([e.data for e in h.events("error")])
    restart = h.events("session_state")[1].data
    assert restart["restart"] == {"count": 2, "resumed": True}
    assert sorted(p["data"]["kind"] for p in h.hook_posts) == ["exited", "fatal", "restart"]
    await r.close()


async def test_hook_results_emitted(tmp_path):
    h = Harness(tmp_path, hooks={"onTurn": [webhook()]})
    await h.router.submit_turn("hi")
    (ev,) = h.events("hook_result")
    assert ev.data["event"] == "onTurn" and ev.data["ok"] is True
    await h.router.close()


async def test_hook_result_event_carries_output_and_invalid_decision(tmp_path):
    from claude_code_wrapper.hooks import ActionResult

    h = Harness(tmp_path, hooks={})
    h.router.on_hook_result(
        ActionResult("onTurn", 0, "command", ok=True, output={"a": 1}, invalid_decision="block")
    )
    h.router.on_hook_result(ActionResult("onTurn", 1, "command", ok=True))
    first, second = h.events("hook_result")
    assert first.data["output"] == {"a": 1} and first.data["invalid_decision"] == "block"
    assert "output" not in second.data and "invalid_decision" not in second.data
    await h.router.close()


async def test_close_emits_session_closed_once(h):
    await h.router.handle_claude_hook("UserPromptSubmit", {})
    await h.router.close()
    await h.router.close()
    assert len(h.events("session_closed")) == 1


async def test_close_drain_is_bounded(tmp_path):
    slow = {"type": "command", "run": "sleep 30", "blocking": True, "timeout_seconds": 30}
    h = Harness(tmp_path, hooks={"onError": [slow]})
    h.router.on_exit(137)  # a background onError fire that would hold close for 30 s
    started = time.monotonic()
    await h.router.close(drain_timeout=0.2)
    assert time.monotonic() - started < 1.0
    assert len(h.events("session_closed")) == 1
    await h.dispatcher.drain(0)


async def test_callback_url_receives_record(tmp_path):
    h = Harness(tmp_path)
    h.session.complete_result = completed_record(callbackUrl="http://turn.cb/rec")
    await h.router.handle_claude_hook("Stop", {})
    await h.router.drain()
    (req,) = [r for r in h.posts if r.url.host == "turn.cb"]
    assert json.loads(req.content)["data"]["record"]["state"] == "completed"
    assert req.headers["x-ccw-signature"].startswith("sha256=")
    await h.router.close()


async def test_assistant_text_fires_on_agent_response(tmp_path):
    h = Harness(tmp_path, hooks={"onAgentResponse": [webhook()]})
    h.router.ensure_tailing()
    h.transcript.write_text(
        tx_line(
            type="assistant",
            message={"id": "m1", "model": "x", "content": [{"type": "text", "text": "hello"}]},
        )
    )
    await wait_until(lambda: h.hook_posts)
    assert h.hook_posts[0]["event"] == "onAgentResponse"
    await h.router.close()


async def test_fatal_fires_on_error(tmp_path):
    h = Harness(tmp_path, hooks={"onError": [webhook()]})
    h.router.on_fatal("auth failed", "tail")
    await h.router.drain()
    assert h.events("error")[0].data["kind"] == "fatal"
    assert h.hook_posts[0]["event"] == "onError" and h.hook_posts[0]["data"]["kind"] == "fatal"
    await h.router.close()


# ---------------------------------------------------------------- ccw commands (D2)
async def test_router_commands_default_and_override(tmp_path):
    from claude_code_wrapper.config import CommandsConfig

    h = Harness(tmp_path)
    assert h.router.commands_cfg is h.cfg.sessions.commands
    assert h.router.commands.cfg is h.router.commands_cfg
    custom = CommandsConfig(agent=["compact"])
    r = SessionRouter(
        None, h.log, h.dispatcher, h.redactor, h.cfg, session_info=h.router.info, commands=custom
    )
    assert r.commands_cfg is custom and r.commands.cfg is custom
    await r.close()
    await h.router.close()


async def test_ccw_tool_denied_for_subagent_before_user_hook(tmp_path):
    h = Harness(tmp_path, hooks={"onAgentToolCall": [webhook()]})
    r = await h.router.handle_claude_hook(
        "PreToolUse",
        {
            "tool_use_id": "tu9",
            "tool_name": "mcp__ccw__command",
            "tool_input": {"name": "compact"},
            "agent_id": "ag1",
            "agent_type": "Explore",
        },
    )
    out = json.loads(r["stdout"])["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == (
        "ccw commands are available to the main agent only; "
        "subagents and teammates cannot call mcp__ccw__command."
    )
    (ev,) = h.events("tool_denied")
    assert ev.data["agent_id"] == "ag1" and ev.data["agent_type"] == "Explore"
    assert ev.data["tool_name"] == "mcp__ccw__command"
    assert h.hook_posts == [] and not h.events("tool_call")
    await h.router.close()


async def test_ccw_tool_allowed_for_main_agent(h):
    r = await h.router.handle_claude_hook(
        "PreToolUse", {"tool_use_id": "t", "tool_name": "mcp__ccw__command", "tool_input": {}}
    )
    assert r["stdout"] == ""
    assert h.events("tool_call") and not h.events("tool_denied")
