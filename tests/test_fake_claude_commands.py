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


def keys(e):
    return jsonl_of(Path(e["CLAUDE_CONFIG_DIR"]) / "fake-keys.jsonl")


def jsonl_of(f):
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


#: The real echo body (claude 2.1.304 transcripts in the probe-slash scratchpad): name,
#: message and args, the last two on lines indented 12 spaces.
INDENT = "\n" + " " * 12


def cmd_content(name, args=""):
    return (
        f"<command-name>/{name}</command-name>{INDENT}<command-message>{name}</command-message>"
        f"{INDENT}<command-args>{args}</command-args>"
    )


def echo_of(line):
    """(style, content) of an echo line: a user line's string content or a system line's."""
    if line["type"] == "system":
        assert line["subtype"] == "local_command"
        return "system", line["content"]
    return "user", line["message"]["content"]


def test_compact_writes_boundary_and_post_compact(boot, env, work, tmp_path):  # noqa: F811
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


def test_slash_command_fires_no_stop(boot, env, tmp_path):  # noqa: F811
    out = tmp_path / "h.out"
    record(env["CLAUDE_CONFIG_DIR"], out)
    p = boot()
    p.submit("/reload-skills", wait="Reloaded skills")
    assert events_of(out) == []


def test_pre_compact_payload_and_block(boot, env, work, tmp_path):  # noqa: F811
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


def test_compact_hang_cancelled_by_esc(boot):
    p = boot({"compact": {"outcome": "hang"}})
    since = len(p.buf)
    p.send(b"\x1b[200~/compact\x1b[201~")
    time.sleep(0.1)
    p.send(b"\r")
    p.pump(0.6)
    assert "Compaction canceled." not in p.buf[since:]
    p.send(b"\x1b")
    p.wait_for("Compaction canceled.", since=since)


def test_compact_without_post_compact_hook(boot, env, work, tmp_path):  # noqa: F811
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
    ("line", "default", "style"),
    [
        ("/reload-plugins", RELOAD, "user"),
        ("/reload-plugins --force", RELOAD, "user"),
        ("/reload-skills", "Reloaded skills", "user"),
        ("/context", "Context usage: 0 tokens", "system"),
        ("/mcp reconnect all", "Reconnected to all MCP servers", "user"),
        ("/mcp", "Reconnected to all MCP servers", "user"),
        ("/mcp reconnect ccw", "Reconnected to ccw", "user"),
    ],
)
def test_other_commands_write_local_stdout(boot, env, work, line, default, style):  # noqa: F811
    p = boot()
    p.submit(line, wait=default)
    lines = transcript_lines(env["CLAUDE_CONFIG_DIR"], work)
    name, _, args = line[1:].partition(" ")
    assert echo_of(lines[0]) == (style, cmd_content(name, args))
    assert lines[1]["type"] == "system" and lines[1]["subtype"] == "local_command"
    assert lines[1]["content"] == f"<local-command-stdout>{default}</local-command-stdout>"
    assert len(lines) == 2


def test_reload_plugins_stdout_and_force_hint(boot, env, work):  # noqa: F811
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


def informational(lines):
    out = []
    for x in lines:
        assert x["type"] == "system" and x["subtype"] == "informational", x
        out.append(x["content"])
    return out


def test_unknown_command(boot, env, work, tmp_path):  # noqa: F811
    """Real sample: summaries/outC.txt:15-16; the binary adds ". Did you mean /y?" for a near miss
    and the "Args from unknown skill" line only when args were typed."""
    out = tmp_path / "h.out"
    record(env["CLAUDE_CONFIG_DIR"], out)
    p = boot()
    p.submit("/nonexistent some args", wait="Unknown command: /nonexistent")
    p.pump(0.3)
    lines = transcript_lines(env["CLAUDE_CONFIG_DIR"], work)
    assert informational(lines) == [
        "Unknown command: /nonexistent",
        "Args from unknown skill: some args",
    ]
    p.submit("/frobnicate", wait="Unknown command: /frobnicate")
    p.pump(0.3)
    lines = transcript_lines(env["CLAUDE_CONFIG_DIR"], work)
    assert informational(lines[2:]) == ["Unknown command: /frobnicate"]
    assert events_of(out) == []


def test_unknown_command_suggestion_form(spawn, scenario_file, env, work):  # noqa: F811
    sc = {"boot": {"bypass_dialog": False}, "suggest": {"comapct": "compact"}}
    p = spawn(FAKE_CLAUDE_SCENARIO=str(scenario_file(sc)))
    p.wait_for("? for shortcuts")
    p.submit("/comapct", wait="Did you mean /compact?")
    lines = transcript_lines(env["CLAUDE_CONFIG_DIR"], work)
    assert informational(lines) == ["Unknown command: /comapct. Did you mean /compact?"]


def test_scenario_listed_name_is_known(boot, env, work):  # noqa: F811
    p = boot({"model": {"stdout": "Set model to opus"}})
    p.submit("/model opus", wait="Set model to opus")
    lines = transcript_lines(env["CLAUDE_CONFIG_DIR"], work)
    assert echo_of(lines[0]) == ("user", cmd_content("model", "opus"))
    assert lines[1]["content"] == "<local-command-stdout>Set model to opus</local-command-stdout>"
    assert len(lines) == 2


def test_echo_styles_and_key_log(boot, env, work):  # noqa: F811
    """Real samples: /context and dialog commands echo as system lines, /reload-plugins and /exit
    as user lines; /cost echoes /usage. ESC keys go to fake-keys.jsonl, never fake-inputs.jsonl."""
    cost = {"echo": "system", "echo_name": "usage", "stdout": "Total cost: $0.0000"}
    p = boot({"cost": cost, "reload-skills": {"echo": "system", "hang": True}})
    p.submit("/reload-plugins", wait=RELOAD)
    p.submit("/context", wait="Context usage")
    p.submit("/cost", wait="Total cost")
    lines = transcript_lines(env["CLAUDE_CONFIG_DIR"], work)
    echoes = [echo_of(x) for x in lines if "<command-name>" in json.dumps(x)]
    assert echoes == [
        ("user", cmd_content("reload-plugins")),
        ("system", cmd_content("context")),
        ("system", cmd_content("usage")),
    ]
    assert keys(env) == []
    since = len(p.buf)
    p.send(b"\x1b")  # at idle
    p.pump(0.3)
    p.send(b"\x1b[200~/reload-skills\x1b[201~")
    time.sleep(0.1)
    p.send(b"\r")
    p.pump(0.3)
    p.send(b"\x1b")  # releases the hung command
    p.wait_for("? for shortcuts", since=since)
    lines = transcript_lines(env["CLAUDE_CONFIG_DIR"], work)
    assert echo_of(lines[-1]) == ("system", cmd_content("reload-skills"))
    got = keys(env)
    assert [k["key"] for k in got] == ["esc", "esc"] and all("ts" in k for k in got)
    rows = inputs(env)
    assert [r["text"] for r in rows] == ["/reload-plugins", "/context", "/cost", "/reload-skills"]
    assert {r["state"] for r in rows} == {"idle"}


def test_input_while_busy_recorded(boot, env):  # noqa: F811
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


def test_compact_summary_quotes_prior_prompts(boot, env, work):  # noqa: F811
    p = boot()
    p.submit("ccw turn marker-A first", wait="? for shortcuts")
    p.submit("ccw turn marker-B second", wait="? for shortcuts")
    p.submit("/compact", wait="Compacted")
    lines = transcript_lines(env["CLAUDE_CONFIG_DIR"], work)
    [summary] = [x for x in lines if x.get("isCompactSummary")]
    text = summary["message"]["content"]
    assert "All user messages:" in text
    assert "ccw turn marker-A first" in text and "ccw turn marker-B second" in text
    assert text.index("marker-A") < text.index("marker-B")


def test_dialog_scenario(boot, env, work):  # noqa: F811
    """Real samples: summaries/outA.txt (/model and /config write their echo only after ESC,
    /config needs two ESCs) and outC.txt (/usage: "Settings dialog dismissed")."""
    p = boot(
        {
            "usage": {
                "dialog": {
                    "footer": "Esc to cancel",
                    "escs": 1,
                    "dismiss_stdout": "Settings dialog dismissed",
                }
            },
            "config": {
                "dialog": {
                    "footer": "Esc to clear",
                    "escs": 2,
                    "dismiss_stdout": "Disabled auto-compact",
                }
            },
            "help": {"dialog": {"footer": "Esc to cancel", "escs": "never"}},
        }
    )

    def open_dialog(line, footer):
        since = len(p.buf)
        p.send(f"\x1b[200~{line}\x1b[201~".encode())
        time.sleep(0.1)
        p.send(b"\r")
        p.wait_for(footer, since=since)
        p.pump(0.3)
        assert "? for shortcuts" not in p.buf[since:]

    def esc_shows(text):
        since = len(p.buf)
        p.send(b"\x1b")
        p.wait_for(text, since=since)
        return since

    def lines():
        try:
            return transcript_lines(env["CLAUDE_CONFIG_DIR"], work)
        except IndexError:  # no transcript file yet
            return []

    open_dialog("/usage", "Esc to cancel")
    assert lines() == []  # nothing is written while the dialog is open
    since = esc_shows("Settings dialog dismissed")
    p.wait_for("? for shortcuts", since=since)
    assert [echo_of(x) for x in lines()[:1]] == [("system", cmd_content("usage"))]
    assert lines()[1]["type"] == "system" and lines()[1]["subtype"] == "local_command"
    assert lines()[1]["content"] == (
        "<local-command-stdout>Settings dialog dismissed</local-command-stdout>"
    )
    assert len(lines()) == 2

    open_dialog("/config", "Esc to clear")
    esc_shows("Esc to clear")  # the first ESC leaves it open: the footer is repainted
    p.pump(0.3)
    assert len(lines()) == 2
    esc_shows("Disabled auto-compact")
    assert echo_of(lines()[2]) == ("system", cmd_content("config"))
    assert (
        lines()[3]["content"]
        == "<local-command-stdout>Disabled auto-compact</local-command-stdout>"
    )

    open_dialog("/help", "Esc to cancel")
    for _ in range(3):
        esc_shows("Esc to cancel")  # never closes: the footer stays on screen
    p.pump(0.3)
    assert len(lines()) == 4
    p.send(b"typed into the dialog")
    p.pump(0.3)
    assert [k["key"] for k in keys(env)] == ["esc"] * 6
    rows = inputs(env)
    assert [r["text"] for r in rows if r["state"] == "idle"] == ["/usage", "/config", "/help"]
    assert [r["text"] for r in rows if r["state"] == "busy"] == ["typed into the dialog"]
