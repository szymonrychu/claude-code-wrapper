"""Transcript reader: path helpers, typed events, usage totals, tailer, subagent watcher."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path

from claude_code_wrapper.redact import Redactor
from claude_code_wrapper.transcript import (
    PROJECT_DIR_NAME,
    AssistantText,
    Interrupted,
    ProbeMarker,
    SubagentWatcher,
    Thinking,
    ToolResult,
    ToolUse,
    TranscriptTailer,
    Usage,
    UsageTotals,
    UserPrompt,
    mangle_cwd,
    parse_line,
    transcript_path,
)

FIX = Path(__file__).parent / "fixtures" / "transcripts"
FAKE = Path(__file__).parent / "fake_claude" / "fake_claude.py"


def parse_file(path: Path, **kw):
    events = []
    for line in path.read_text().splitlines():
        events.extend(parse_line(line, **kw))
    return events


def jl(obj: dict) -> str:
    return json.dumps(obj)


# ---------------------------------------------------------------- paths
def test_transcript_path_pinned_dir(tmp_path):
    assert PROJECT_DIR_NAME == "workdir"
    assert transcript_path(tmp_path, "abc") == tmp_path / "projects" / "workdir" / "abc.jsonl"
    assert transcript_path(tmp_path, "abc", "x") == tmp_path / "projects" / "x" / "abc.jsonl"
    assert mangle_cwd("/tmp/a.b_c/d") == "-tmp-a-b-c-d"
    assert mangle_cwd(Path("/workspace")) == "-workspace"


# ---------------------------------------------------------------- parse_line
def test_real_fixture_lines_parse():
    events = parse_file(FIX / "basic.jsonl")
    kinds = {type(e) for e in events}
    assert {UserPrompt, AssistantText, Thinking, ToolUse, ToolResult, Usage, Interrupted} <= kinds
    assert ProbeMarker in kinds
    prompts = [e for e in events if isinstance(e, UserPrompt)]
    assert [p.turn_marker for p in prompts if p.turn_marker] == ["turn-0001", "turn-0002"]
    assert any(e.text == "PONG" for e in events if isinstance(e, AssistantText))
    assert [e.probe_id for e in events if isinstance(e, ProbeMarker)] == ["probe-0001"]
    use = next(e for e in events if isinstance(e, ToolUse) and e.name == "Bash")
    assert use.input["command"] == "sleep 10"
    res = next(e for e in events if isinstance(e, ToolResult) and e.is_error)
    assert res.tool_use_id == use.id
    usage = next(e for e in events if isinstance(e, Usage) and e.message_id.startswith("msg_012iq"))
    assert (usage.model, usage.input_tokens, usage.output_tokens) == ("claude-opus-4-8", 1658, 5)
    assert usage.cache_read_input_tokens == 16218
    assert usage.cache_creation_input_tokens == 1730
    assert not any(e.sidechain for e in events)


def test_parse_real_subagent_fixture_is_sidechain():
    events = parse_file(FIX / "subagent.jsonl", agent_id="a04c938e71f35e132", sidechain=True)
    assert events
    assert all(e.sidechain and e.agent_id == "a04c938e71f35e132" for e in events)
    kinds = {type(e) for e in events}
    assert {UserPrompt, ToolUse, ToolResult, AssistantText, Thinking, Usage} <= kinds


def test_line_flags_set_sidechain_and_agent_id():
    line = jl(
        {
            "type": "assistant",
            "isSidechain": True,
            "agentId": "zz",
            "message": {"id": "m", "content": [{"type": "text", "text": "hi"}]},
        }
    )
    (ev,) = parse_line(line)
    assert ev.sidechain is True and ev.agent_id == "zz"


def test_malformed_line_skipped(caplog):
    for t in ("attachment", "system", "queue-operation", "file-history-snapshot", "summary", "x"):
        assert parse_line(jl({"type": t, "message": {"content": "hi"}})) == []
    with caplog.at_level("DEBUG"):
        assert parse_line("{not json") == []
        assert parse_line("") == []
        assert parse_line("[1,2]") == []
    assert parse_line(jl({"type": "user", "message": "weird"})) == []
    assert parse_line(jl({"type": "assistant", "message": {"content": [7, {"type": "new"}]}})) == []


def test_parse_tool_result_error_truncated():
    blocks = [{"type": "text", "text": "a"}, {"type": "image"}, {"type": "text", "text": "b"}]
    line = jl(
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": blocks},
                    {"type": "tool_result", "tool_use_id": "t2", "content": "x" * 5000},
                ]
            },
        }
    )
    r1, r2 = parse_line(line)
    assert (r1.text, r1.is_error, r1.tool_use_id) == ("a\nb", False, "t1")
    assert r2.text == "x" * 4000 + "...[truncated]"


def test_interrupt_line_detected():
    s = jl({"type": "user", "message": {"content": "[Request interrupted by user]"}})
    b = jl(
        {
            "type": "user",
            "message": {
                "content": [{"type": "text", "text": "[Request interrupted by user for tool use]"}]
            },
        }
    )
    assert parse_line(s) == [Interrupted()]
    assert parse_line(b) == [Interrupted()]


def test_turn_marker_extracted():
    p = jl({"type": "user", "message": {"content": "hello\n<!-- ccw turn: turn-0007 -->"}})
    (ev,) = parse_line(p)
    assert ev == UserPrompt(text="hello\n<!-- ccw turn: turn-0007 -->", turn_marker="turn-0007")
    plain = parse_line(jl({"type": "user", "message": {"content": "hello"}}))
    assert plain == [UserPrompt(text="hello", turn_marker=None)]
    probe = parse_line(jl({"type": "user", "message": {"content": "<!-- ccw probe: p-1 -->"}}))
    assert probe == [ProbeMarker(probe_id="p-1")]


def queued_command(prompt: str, **attachment: object) -> str:
    """A `queued_command` attachment line shaped like the real ones (an absorbed prompt)."""
    body = {
        "type": "queued_command",
        "prompt": prompt,
        "commandMode": "prompt",
        "origin": {"kind": "human"},
        "humanTurn": True,
    }
    return jl(
        {
            "type": "attachment",
            "isSidechain": False,
            "attachment": {**body, **attachment},
            "timestamp": "2026-10-28T16:07:48.399Z",
        }
    )


def test_queued_command_attachment_yields_the_user_prompt_event():
    prompt = '<pasted_content id="1">\nhello\n</pasted_content>\n\n<!-- ccw turn: turn-0abc -->'
    absorbed = parse_line(queued_command(prompt))
    plain = parse_line(jl({"type": "user", "message": {"role": "user", "content": prompt}}))
    assert absorbed == plain == [UserPrompt(text=prompt, turn_marker="turn-0abc")]
    # The same classification as a user line: probes, interrupts and commands keep their kind.
    probe = parse_line(queued_command("<!-- ccw probe: p-1 -->"))
    assert probe == [ProbeMarker(probe_id="p-1")]
    (sub,) = parse_line(queued_command("hi"), sidechain=True, agent_id="a1")
    assert (sub.sidechain, sub.agent_id) == (True, "a1")
    red = parse_line(
        queued_command("key supersecret-value"), redactor=Redactor(["supersecret-value"])
    )
    assert "supersecret-value" not in red[0].text


def test_queued_command_attachment_without_a_prompt_yields_nothing():
    for attachment in ({}, {"prompt": None}, {"prompt": 5}, {"prompt": ["x"]}):
        line = jl({"type": "attachment", "attachment": {"type": "queued_command", **attachment}})
        assert parse_line(line) == []
    assert parse_line(jl({"type": "attachment", "attachment": "queued_command"})) == []
    assert parse_line(jl({"type": "attachment"})) == []


def test_other_attachment_types_yield_nothing():
    for kind in (
        "task_reminder",
        "diagnostics",
        "hook_success",
        "edited_text_file",
        "command_permissions",
    ):
        line = jl(
            {
                "type": "attachment",
                "attachment": {
                    "type": kind,
                    "prompt": "x <!-- ccw turn: turn-0abc -->",
                    "content": "y",
                },
            }
        )
        assert parse_line(line) == []


def test_text_redacted():
    red = Redactor(["supersecret-value"])
    line = jl(
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "model": "m",
                "content": [
                    {"type": "text", "text": "key supersecret-value"},
                    {"type": "thinking", "thinking": "supersecret-value hmm"},
                    {
                        "type": "tool_use",
                        "id": "t",
                        "name": "Bash",
                        "input": {"c": "supersecret-value"},
                    },
                ],
            },
        }
    )
    ev = parse_line(line, redactor=red)
    assert ev[0].text == "key ***"
    assert ev[1].text == "*** hmm"
    assert ev[2].input == {"c": "***"}
    res = parse_line(
        jl(
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t", "content": "supersecret-value"}
                    ]
                },
            }
        ),
        redactor=red,
    )
    assert res[0].text == "***"
    up = parse_line(jl({"type": "user", "message": {"content": "supersecret-value"}}), redactor=red)
    assert up[0].text == "***"


def test_usage_emitted_once_per_line_with_message_fields():
    line = jl(
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "model": "mod",
                "content": [{"type": "text", "text": "a"}],
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        }
    )
    evs = parse_line(line)
    assert evs == [
        AssistantText(text="a", message_id="m1", model="mod"),
        Usage(
            message_id="m1",
            model="mod",
            input_tokens=1,
            output_tokens=2,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    ]


# ---------------------------------------------------------------- UsageTotals
def test_usage_deduped_by_message_id():
    t = UsageTotals()
    assert t.last_usage is None and t.as_dict() == {}
    a = Usage("m1", "opus", 10, 5, 100, 7)
    assert t.add(a) is True
    assert t.add(a) is False  # repeated per content block
    t.add(Usage("m2", "opus", 1, 1, 1, 1))
    t.add(Usage("m3", "haiku", 2, 3, 0, 0))
    assert t.as_dict() == {
        "opus": {
            "input_tokens": 11,
            "output_tokens": 6,
            "cache_read_input_tokens": 101,
            "cache_creation_input_tokens": 8,
        },
        "haiku": {
            "input_tokens": 2,
            "output_tokens": 3,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }
    assert t.last_usage == Usage("m3", "haiku", 2, 3, 0, 0)
    t.reset()
    assert t.as_dict() == {} and t.last_usage is None
    assert t.add(a) is True


def test_fixture_usage_totals_dedupe():
    usages = [e for e in parse_file(FIX / "basic.jsonl") if isinstance(e, Usage)]
    repeated = [u for u in usages if u.message_id == "msg_011CdsZwAiFdbnfv6TrPyTGS"]
    assert len(repeated) == 2  # claude repeats usage per content block
    t = UsageTotals()
    for u in usages:
        t.add(u)
    unique = {u.message_id: u for u in usages if u.model == "claude-opus-5"}
    assert t.as_dict()["claude-opus-5"]["output_tokens"] == sum(
        u.output_tokens for u in unique.values()
    )


# ---------------------------------------------------------------- fake claude
def _load_fake():
    spec = importlib.util.spec_from_file_location("fake_claude_mod", FAKE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fake_claude_lines_parse(tmp_path):
    mod = _load_fake()
    fake = object.__new__(mod.Fake)
    fake.cwd = "/work"
    fake.session_id = "sid"
    fake.proj_dir = str(tmp_path / "projects" / "workdir")
    fake.transcript = os.path.join(fake.proj_dir, "sid.jsonl")
    fake.user_line("do it\n<!-- ccw turn: turn-0001 -->")
    fake.assistant_line({"type": "thinking", "thinking": "hmm"}, 3)
    fake.assistant_line(
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}
    )
    fake.user_line(
        [{"type": "tool_result", "tool_use_id": "t1", "content": "out", "is_error": False}]
    )
    fake.assistant_line({"type": "text", "text": "done"})
    fake.user_line([{"type": "text", "text": "[Request interrupted by user]"}])
    fake_events = parse_file(Path(fake.transcript))
    sub = os.path.join(fake.proj_dir, "sid", "subagents", "agent-q1.jsonl")
    fake.user_line("subagent task", sub, sidechain=True)
    fake.assistant_line({"type": "text", "text": "sub"}, 0, sub, True)
    sub_events = parse_file(Path(sub))

    def kinds(evs):
        return {type(e) for e in evs}

    real = kinds(parse_file(FIX / "basic.jsonl"))
    assert kinds(fake_events) <= real
    assert {UserPrompt, Thinking, ToolUse, ToolResult, AssistantText, Usage, Interrupted} == kinds(
        fake_events
    )
    assert fake_events[0].turn_marker == "turn-0001"
    assert all(e.sidechain for e in sub_events)
    assert kinds(sub_events) <= kinds(parse_file(FIX / "subagent.jsonl", sidechain=True))


# ---------------------------------------------------------------- tailer
class Sink:
    def __init__(self):
        self.events: list = []
        self.batches = 0

    def __call__(self, evs):
        self.batches += 1
        self.events.extend(evs)

    async def wait_for(self, n: int, timeout: float = 5.0):
        end = asyncio.get_running_loop().time() + timeout
        while len(self.events) < n:
            if asyncio.get_running_loop().time() > end:
                raise AssertionError(f"got {len(self.events)} events, wanted {n}: {self.events}")
            await asyncio.sleep(0.01)


def text_line(text: str, mid: str = "m") -> str:
    return (
        jl(
            {
                "type": "assistant",
                "message": {"id": mid, "content": [{"type": "text", "text": text}]},
            }
        )
        + "\n"
    )


async def start(tailer: TranscriptTailer) -> asyncio.Task:
    return asyncio.create_task(tailer.run())


async def test_tailer_waits_for_file(tmp_path):
    path = tmp_path / "p" / "s.jsonl"
    sink = Sink()
    t = TranscriptTailer(path, sink, poll_interval=0.01)
    task = await start(t)
    await asyncio.sleep(0.05)
    path.parent.mkdir()
    path.write_text(text_line("one"))
    await sink.wait_for(1)
    with path.open("a") as f:
        f.write(text_line("two"))
    await sink.wait_for(2)
    assert [e.text for e in sink.events] == ["one", "two"]
    t.stop()
    await asyncio.wait_for(task, 2)


async def test_tailer_follows_appends_and_partial_lines(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text("")
    sink = Sink()
    t = TranscriptTailer(path, sink, poll_interval=0.01)
    task = await start(t)
    full = text_line("whole")
    with path.open("a") as f:
        f.write(full[:20])
        f.flush()
    await asyncio.sleep(0.1)
    assert sink.events == []
    with path.open("a") as f:
        f.write(full[20:])
    await sink.wait_for(1)
    assert sink.events[0].text == "whole"
    t.stop()
    await asyncio.wait_for(task, 2)


async def test_tailer_reopens_on_truncate(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(text_line("aaaaaaaaaaaaaaaaaaaaaaaa") + text_line("bbbbbbbbbbbbbbbbbbbbbb"))
    sink = Sink()
    t = TranscriptTailer(path, sink, poll_interval=0.01)
    task = await start(t)
    await sink.wait_for(2)
    path.write_text(text_line("c"))  # shrink: same inode
    await sink.wait_for(3)
    assert sink.events[-1].text == "c"
    replacement = tmp_path / "new.jsonl"
    replacement.write_text(text_line("dddddddddddddddddddddddddddddddddddddddddddd"))
    os.replace(replacement, path)  # new inode, larger file
    await sink.wait_for(4)
    assert sink.events[-1].text.startswith("ddd")
    t.stop()
    await asyncio.wait_for(task, 2)


async def test_tailer_switch_follows_other_file_from_start(tmp_path):
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    a.write_text(text_line("from-a"))
    b.write_text(text_line("b1") + text_line("b2"))
    sink = Sink()
    t = TranscriptTailer(a, sink, poll_interval=0.01)
    task = await start(t)
    await sink.wait_for(1)
    t.switch(b)
    await sink.wait_for(3)
    assert [e.text for e in sink.events] == ["from-a", "b1", "b2"]
    t.stop()
    await asyncio.wait_for(task, 2)


async def test_flush_delivers_pending_lines(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(text_line("x"))
    sink = Sink()
    t = TranscriptTailer(path, sink, poll_interval=60)  # never polls on its own in the test
    await t.flush()
    assert [e.text for e in sink.events] == ["x"]
    with path.open("a") as f:
        f.write(text_line("y") + "partial")
    await t.flush()
    assert [e.text for e in sink.events] == ["x", "y"]
    await t.flush()
    assert len(sink.events) == 2


async def test_tailer_discards_oversize_line_with_warning(tmp_path, monkeypatch, caplog):
    import claude_code_wrapper.transcript as tr

    monkeypatch.setattr(tr, "MAX_LINE_BYTES", 1000)
    path = tmp_path / "s.jsonl"
    path.write_text("")
    sink = Sink()
    t = TranscriptTailer(path, sink, poll_interval=60)
    with path.open("a") as f:
        f.write("x" * 3000)
    with caplog.at_level("WARNING"):
        await t.flush()
        with path.open("a") as f:
            f.write("y" * 10 + "\n" + text_line("after"))
        await t.flush()
    assert [e.text for e in sink.events] == ["after"]
    assert any("16 MiB" in r.message or "discard" in r.message for r in caplog.records)


async def test_tailer_passes_sidechain_agent_and_redactor(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(text_line("tok-123456789"))
    sink = Sink()
    t = TranscriptTailer(
        path, sink, redactor=Redactor(["tok-123456789"]), sidechain=True, agent_id="a1"
    )
    await t.flush()
    assert sink.events[0].text == "***"
    assert sink.events[0].sidechain and sink.events[0].agent_id == "a1"


async def test_tailer_async_callback_and_callback_error_survives(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(text_line("one"))
    got = []
    calls = 0

    async def cb(evs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        got.extend(evs)

    t = TranscriptTailer(path, cb, poll_interval=60)
    await t.flush()
    with path.open("a") as f:
        f.write(text_line("two"))
    await t.flush()
    assert [e.text for e in got] == ["two"]


# ---------------------------------------------------------------- subagent watcher
async def test_subagent_watcher_tags_sidechain(tmp_path):
    main = tmp_path / "projects" / "workdir" / "sid.jsonl"
    main.parent.mkdir(parents=True)
    main.write_text("")
    sink = Sink()
    w = SubagentWatcher(main, sink, poll_interval=0.01)
    task = asyncio.create_task(w.run())
    await asyncio.sleep(0.05)
    subdir = main.parent / "sid" / "subagents"
    subdir.mkdir(parents=True)
    (subdir / "agent-abc123.jsonl").write_text(text_line("s1"))
    (subdir / "agent-def.jsonl").write_text(text_line("s2"))
    (subdir / "agent-abc123.meta.json").write_text("{}")  # not a transcript
    await sink.wait_for(2)
    assert {(e.text, e.agent_id, e.sidechain) for e in sink.events} == {
        ("s1", "abc123", True),
        ("s2", "def", True),
    }
    with (subdir / "agent-abc123.jsonl").open("a") as f:
        f.write(text_line("s3"))
    await sink.wait_for(3)
    assert len(w.agent_ids) == 2
    w.stop()
    await asyncio.wait_for(task, 2)


async def test_subagent_watcher_flush_and_real_fixture(tmp_path):
    main = tmp_path / "projects" / "workdir" / "sid.jsonl"
    subdir = main.parent / "sid" / "subagents"
    subdir.mkdir(parents=True)
    (subdir / "agent-a04c938e71f35e132.jsonl").write_text((FIX / "subagent.jsonl").read_text())
    sink = Sink()
    w = SubagentWatcher(main, sink, poll_interval=60)
    await w.flush()
    assert sink.events and all(
        e.agent_id == "a04c938e71f35e132" and e.sidechain for e in sink.events
    )
    w.stop()


def test_parse_assistant_text_and_tool_use():
    line = jl(
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "model": "claude-opus-4-8",
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "ls"}},
                ],
            },
        }
    )
    events = parse_line(line)
    text = next(e for e in events if isinstance(e, AssistantText))
    assert (text.text, text.message_id, text.model) == ("hello", "m1", "claude-opus-4-8")
    use = next(e for e in events if isinstance(e, ToolUse))
    assert (use.id, use.name, use.input) == ("tu1", "Bash", {"command": "ls"})
