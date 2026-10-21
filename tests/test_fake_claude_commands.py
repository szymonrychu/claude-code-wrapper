"""Fake claude local slash commands (docstring of tests/fake_claude/fake_claude.py is the spec)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from test_fake_claude import (  # type: ignore[import-not-found]
    env,  # noqa: F401
    hook_settings,
    payloads,
    spawn,  # noqa: F401
    transcript_lines,
    work,  # noqa: F401
)

ALL_EVENTS = ["UserPromptSubmit", "Stop", "PreCompact", "PostCompact"]


def record(config_dir, out, events=ALL_EVENTS):
    hook_settings(
        config_dir,
        {
            ev: [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": f"cat >> {out}; echo >> {out}"}],
                }
            ]
            for ev in events
        },
    )


@pytest.fixture
def boot(spawn, scenario_file):  # noqa: F811
    def _boot(commands=None, turns=None):
        sc = {"boot": {"bypass_dialog": False}, "turns": turns or [], "commands": commands or {}}
        p = spawn(FAKE_CLAUDE_SCENARIO=str(scenario_file(sc)))
        p.wait_for("? for shortcuts")
        return p

    return _boot


def events_of(out):
    return [p["hook_event_name"] for p in payloads(out)]


def inputs(e):
    f = Path(e["CLAUDE_CONFIG_DIR"]) / "fake-inputs.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines()]


def cmd_content(name, args=""):
    return (
        f"<command-name>/{name}</command-name>\n<command-message>{name}</command-message>\n"
        f"<command-args>{args}</command-args>"
    )


def test_compact_ok_writes_boundary_summary_and_hooks(boot, env, work, tmp_path):  # noqa: F811
    out = tmp_path / "h.out"
    record(env["CLAUDE_CONFIG_DIR"], out)
    p = boot()
    p.submit("ccw turn marker-1 hello")
    p.submit("/compact keep the plan", wait="Compacted")
    p.wait_for("? for shortcuts", since=p.buf.rindex("Compacted"))
    lines = transcript_lines(env["CLAUDE_CONFIG_DIR"], work)
    [cmd] = [x for x in lines if "<command-name>" in json.dumps(x)]
    assert cmd["message"]["content"] == cmd_content("compact", "keep the plan")
    [boundary] = [x for x in lines if x.get("subtype") == "compact_boundary"]
    assert boundary["type"] == "system" and boundary["content"] == "Conversation compacted"
    meta = boundary["compactMetadata"]
    assert meta["trigger"] == "manual" and {"preTokens", "postTokens", "durationMs"} <= set(meta)
    assert boundary["sessionId"] == lines[0]["sessionId"]
    [summary] = [x for x in lines if x.get("isCompactSummary")]
    assert summary["isVisibleInTranscriptOnly"] is True
    text = summary["message"]["content"]
    assert text.startswith("This session is being continued from a previous conversation")
    assert "All user messages:" in text and "ccw turn marker-1 hello" in text
    assert lines.index(boundary) < lines.index(summary)
    pl = payloads(out)
    assert events_of(out) == ["UserPromptSubmit", "Stop", "PreCompact", "PostCompact"]
    assert pl[2]["trigger"] == "manual" and pl[2]["custom_instructions"] == "keep the plan"
    assert pl[3]["trigger"] == "manual" and pl[3]["compact_summary"] == text
    assert [x["type"] for x in lines].count("assistant") == 1


def test_compact_without_args_has_null_instructions(boot, env, tmp_path):  # noqa: F811
    out = tmp_path / "h.out"
    record(env["CLAUDE_CONFIG_DIR"], out, ["PreCompact"])
    p = boot()
    p.submit("/compact", wait="Compacted")
    assert payloads(out)[0]["custom_instructions"] is None


def test_command_runs_no_turn_hooks(boot, env, tmp_path):  # noqa: F811
    out = tmp_path / "h.out"
    record(env["CLAUDE_CONFIG_DIR"], out)
    p = boot()
    p.submit("/reload-skills", wait="Reloaded skills")
    assert events_of(out) == []


def test_pre_compact_block_cancels(boot, env, work, tmp_path):  # noqa: F811
    out = tmp_path / "h.out"
    hook_settings(
        env["CLAUDE_CONFIG_DIR"],
        {
            "PreCompact": [{"hooks": [{"type": "command", "command": "echo no >&2; exit 2"}]}],
            "PostCompact": [{"hooks": [{"type": "command", "command": f"cat >> {out}"}]}],
        },
    )
    p = boot()
    p.submit("/compact", wait="Compaction canceled.")
    lines = transcript_lines(env["CLAUDE_CONFIG_DIR"], work)
    assert not any(x.get("subtype") == "compact_boundary" for x in lines)
    assert "<local-command-stderr>Compaction canceled.</local-command-stderr>" in json.dumps(lines)
    assert not out.exists()


def test_pre_compact_json_block_cancels(boot, env):  # noqa: F811
    cmd = 'echo \'{"decision": "block"}\''
    hook_settings(
        env["CLAUDE_CONFIG_DIR"],
        {"PreCompact": [{"hooks": [{"type": "command", "command": cmd}]}]},
    )
    p = boot()
    p.submit("/compact", wait="Compaction canceled.")


@pytest.mark.parametrize(
    ("outcome", "msg"),
    [
        ("canceled", "Compaction canceled."),
        ("not_enough", "Not enough messages to compact."),
        ("error", "Error compacting conversation"),
    ],
)
def test_compact_failure_outcomes(boot, env, work, tmp_path, outcome, msg):  # noqa: F811
    out = tmp_path / "h.out"
    record(env["CLAUDE_CONFIG_DIR"], out, ["PostCompact"])
    p = boot({"compact": {"outcome": outcome}})
    p.submit("/compact", wait=msg)
    lines = transcript_lines(env["CLAUDE_CONFIG_DIR"], work)
    [fail] = [x for x in lines if x.get("subtype") == "local_command"]
    assert fail["type"] == "system"
    assert fail["content"] == f"<local-command-stderr>{msg}</local-command-stderr>"
    assert not any(x.get("subtype") == "compact_boundary" for x in lines)
    assert not out.exists()


def test_compact_hang_until_esc_then_canceled(boot):
    p = boot({"compact": {"outcome": "hang"}})
    since = len(p.buf)
    p.send(b"\x1b[200~/compact\x1b[201~")
    time.sleep(0.1)
    p.send(b"\r")
    p.pump(0.6)
    assert "Compaction canceled." not in p.buf[since:]
    p.send(b"\x1b")
    p.wait_for("Compaction canceled.", since=since)


def test_post_compact_hook_false_skips_hook(boot, env, work, tmp_path):  # noqa: F811
    out = tmp_path / "h.out"
    record(env["CLAUDE_CONFIG_DIR"], out, ["PreCompact", "PostCompact"])
    p = boot({"compact": {"post_compact_hook": False}})
    p.submit("/compact", wait="Compacted")
    assert events_of(out) == ["PreCompact"]
    lines = transcript_lines(env["CLAUDE_CONFIG_DIR"], work)
    assert any(x.get("subtype") == "compact_boundary" for x in lines)


def test_compact_delay(boot):
    p = boot({"compact": {"delay_seconds": 0.8}})
    start = time.monotonic()
    p.submit("/compact", wait="Compacted")
    assert time.monotonic() - start >= 0.7


RELOAD = "Reloaded: 0 plugins, 0 skills, 0 agents, 0 hooks, 0 MCP servers"


@pytest.mark.parametrize(
    ("line", "default"),
    [
        ("/reload-plugins", RELOAD),
        ("/reload-plugins --force", RELOAD),
        ("/reload-skills", "Reloaded skills"),
        ("/context", "Context usage: 0 tokens"),
        ("/mcp reconnect all", "Reconnected to all MCP servers"),
        ("/mcp", "Reconnected to all MCP servers"),
        ("/mcp reconnect ccw", "Reconnected to ccw"),
    ],
)
def test_stdout_commands_defaults(boot, env, work, line, default):  # noqa: F811
    p = boot()
    p.submit(line, wait=default)
    lines = transcript_lines(env["CLAUDE_CONFIG_DIR"], work)
    name, _, args = line[1:].partition(" ")
    assert lines[0]["message"]["content"] == cmd_content(name, args)
    assert lines[1]["type"] == "system" and lines[1]["subtype"] == "local_command"
    assert lines[1]["content"] == f"<local-command-stdout>{default}</local-command-stdout>"
    assert len(lines) == 2


def test_reload_stdout_override_and_as_user(boot, env, work):  # noqa: F811
    msg = "Skipped. Run /reload-plugins --force to apply."
    p = boot({"reload-plugins": {"stdout": msg, "as_user": True}})
    p.submit("/reload-plugins", wait="Run /reload-plugins")
    lines = transcript_lines(env["CLAUDE_CONFIG_DIR"], work)
    assert lines[1]["type"] == "user"
    assert lines[1]["message"]["content"] == f"<local-command-stdout>{msg}</local-command-stdout>"


def test_reload_hang_until_esc(boot, env, work):  # noqa: F811
    p = boot({"reload-plugins": {"hang": True}})
    since = len(p.buf)
    p.send(b"\x1b[200~/reload-plugins\x1b[201~")
    time.sleep(0.1)
    p.send(b"\r")
    p.pump(0.5)
    assert "? for shortcuts" not in p.buf[since:]
    assert len(transcript_lines(env["CLAUDE_CONFIG_DIR"], work)) == 1
    p.send(b"\x1b")
    p.wait_for("? for shortcuts", since=since)
    assert len(transcript_lines(env["CLAUDE_CONFIG_DIR"], work)) == 1


def test_unknown_command(boot, env):  # noqa: F811
    p = boot()
    p.submit("/frobnicate now", wait="Unknown command: /frobnicate")
    p.pump(0.3)
    projects = Path(env["CLAUDE_CONFIG_DIR"]) / "projects"
    assert not list(projects.glob("*/*.jsonl"))


def test_inputs_log_idle_and_busy(boot, env):  # noqa: F811
    p = boot(turns=[{"match": "slow", "steps": [{"sleep": 1.0}, {"text": "done"}]}])
    p.submit("/context", wait="Context usage")
    p.send(b"\x1b[200~slow\x1b[201~")
    time.sleep(0.1)
    p.send(b"\r")
    time.sleep(0.2)
    p.send(b"typed-while-busy")
    p.wait_for("? for shortcuts", since=len(p.buf))
    rows = inputs(env)
    assert [r["text"] for r in rows if r["state"] == "idle"] == ["/context", "slow"]
    busy = [r for r in rows if r["state"] == "busy"]
    assert [r["text"] for r in busy] == ["typed-while-busy"]
    assert all("ts" in r for r in rows)


def test_esc_not_recorded_as_busy(boot, env):  # noqa: F811
    p = boot(turns=[{"match": "slow", "steps": [{"sleep": 5}]}])
    p.send(b"\x1b[200~slow\x1b[201~")
    time.sleep(0.1)
    p.send(b"\r")
    time.sleep(0.3)
    p.send(b"\x1b")
    p.wait_for("[Request interrupted by user]")
    assert [r["state"] for r in inputs(env)] == ["idle"]


def test_auto_compact_step(boot, env, work, tmp_path):  # noqa: F811
    out = tmp_path / "h.out"
    record(env["CLAUDE_CONFIG_DIR"], out, ["PreCompact", "PostCompact", "Stop"])
    steps = [{"text": "before"}, {"auto_compact": {"pre_tokens": 777}}, {"text": "after"}]
    p = boot(turns=[{"match": "big", "steps": steps}])
    p.submit("ccw turn m2 big")
    lines = transcript_lines(env["CLAUDE_CONFIG_DIR"], work)
    [b] = [x for x in lines if x.get("subtype") == "compact_boundary"]
    assert b["compactMetadata"]["trigger"] == "auto" and b["compactMetadata"]["preTokens"] == 777
    assert any(x.get("isCompactSummary") for x in lines)
    texts = [x["message"]["content"][0]["text"] for x in lines if x["type"] == "assistant"]
    assert texts == ["before", "after"]
    pl = payloads(out)
    assert [x["hook_event_name"] for x in pl] == ["PreCompact", "PostCompact", "Stop"]
    assert pl[0]["trigger"] == "auto" and pl[1]["trigger"] == "auto"
    assert "ccw turn m2 big" in pl[1]["compact_summary"]
