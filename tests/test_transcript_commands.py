"""Command echoes, compact boundaries and summaries in the transcript (design D8)."""

import json
from pathlib import Path

from claude_code_wrapper.redact import Redactor
from claude_code_wrapper.transcript import (
    CommandInvoked,
    CommandOutput,
    CommandUnknown,
    CompactBoundary,
    TranscriptTailer,
    UserPrompt,
    parse_line,
)

FIXTURE = Path(__file__).parent / "fixtures" / "transcripts" / "commands.jsonl"
LINES = FIXTURE.read_text().splitlines()


def test_command_echo_is_command_invoked():
    assert parse_line(LINES[0]) == [CommandInvoked(name="compact", args="keep the plan")]


#: The real echo body (claude 2.1.304): name, message and args on lines indented 12 spaces.
INDENT = "\n" + " " * 12


def real_echo(name, args=""):
    return (
        f"<command-name>/{name}</command-name>{INDENT}<command-message>{name}</command-message>"
        f"{INDENT}<command-args>{args}</command-args>"
    )


def test_real_user_echo_is_command_invoked():
    line = json.dumps({"type": "user", "message": {"content": real_echo("reload-plugins")}})
    assert parse_line(line) == [CommandInvoked(name="reload-plugins", args="")]


def test_system_command_echo_is_command_invoked():
    """claude writes the echo of /context and of every dialog command as a system line."""
    for name, args in (("context", ""), ("usage", ""), ("model", "opus")):
        line = json.dumps(
            {"type": "system", "subtype": "local_command", "content": real_echo(name, args)}
        )
        assert parse_line(line) == [CommandInvoked(name=name, args=args)]
    stdout = json.dumps(
        {
            "type": "system",
            "subtype": "local_command",
            "content": "<local-command-stdout>Settings dialog dismissed</local-command-stdout>",
        }
    )
    assert parse_line(stdout) == [CommandOutput(stdout="Settings dialog dismissed", stderr="")]


def informational(content):
    return json.dumps({"type": "system", "subtype": "informational", "content": content})


def test_informational_unknown_command():
    """Real sample: summaries/outC.txt lines 15-16 of the probe-slash scratchpad."""
    assert parse_line(informational("Unknown command: /nonexistent")) == [
        CommandUnknown(name="nonexistent")
    ]
    assert parse_line(informational("Args from unknown skill: some args")) == []
    assert parse_line(informational("Auto mode is now the default")) == []
    events = parse_line(informational("Unknown command: /x"), sidechain=True, agent_id="a1")
    assert events == [CommandUnknown(name="x", sidechain=True, agent_id="a1")]


def test_informational_unknown_command_with_suggestion():
    line = informational("Unknown command: /comapct. Did you mean /compact?")
    assert parse_line(line) == [CommandUnknown(name="comapct")]


def test_command_without_args_has_empty_args():
    line = json.dumps(
        {"type": "user", "message": {"content": "  <command-name>/reload-plugins</command-name>"}}
    )
    assert parse_line(line) == [CommandInvoked(name="reload-plugins", args="")]


def test_command_message_first_in_text_blocks():
    line = json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "text", "text": "<command-message>x</command-message>"},
                    {"type": "text", "text": "<command-name>/x</command-name>"},
                ]
            },
        }
    )
    assert parse_line(line) == [CommandInvoked(name="x", args="")]


def test_command_blocks_are_joined_into_one_event():
    blocks = [
        "<command-message>x</command-message>",
        "<command-name>/x</command-name>",
        "<command-args>do it</command-args>",
    ]
    line = json.dumps(
        {"type": "user", "message": {"content": [{"type": "text", "text": t} for t in blocks]}}
    )
    assert parse_line(line) == [CommandInvoked(name="x", args="do it")]


def test_system_local_command_is_output():
    assert parse_line(LINES[1]) == [CommandOutput(stdout="Compacted", stderr="")]


def test_user_local_command_stdout_is_output():
    assert parse_line(LINES[2]) == [CommandOutput(stdout="Reloaded: 1 plugin", stderr="")]


def test_stderr_body():
    line = json.dumps(
        {
            "type": "user",
            "message": {"content": "<local-command-stderr>boom</local-command-stderr>"},
        }
    )
    assert parse_line(line) == [CommandOutput(stdout="", stderr="boom")]


def test_system_line_with_local_command_prefix_without_subtype():
    line = json.dumps(
        {"type": "system", "content": "<local-command-stdout>ok</local-command-stdout>"}
    )
    assert parse_line(line) == [CommandOutput(stdout="ok", stderr="")]


def test_compact_boundary():
    assert parse_line(LINES[3]) == [
        CompactBoundary(trigger="manual", pre_tokens=150000, post_tokens=9000, duration_ms=4200)
    ]


def test_compact_boundary_defaults():
    line = json.dumps({"type": "system", "subtype": "compact_boundary"})
    assert parse_line(line) == [
        CompactBoundary(trigger="unknown", pre_tokens=0, post_tokens=None, duration_ms=None)
    ]


def test_compact_summary_and_meta_yield_nothing():
    assert parse_line(LINES[4]) == []
    assert parse_line(LINES[5]) == []


def test_other_system_lines_yield_nothing():
    assert parse_line(json.dumps({"type": "system", "subtype": "info", "content": "hi"})) == []


def test_ordinary_prompt_with_tag_mention_keeps_marker():
    events = parse_line(LINES[6])
    assert len(events) == 1 and isinstance(events[0], UserPrompt)
    assert events[0].turn_marker == "t_new"


def test_sidechain_flag_and_redaction():
    events = parse_line(LINES[0], sidechain=True, agent_id="a1")
    assert events[0].sidechain and events[0].agent_id == "a1"
    redactor = Redactor(["hunter2-secret-value"])
    line = json.dumps(
        {
            "type": "user",
            "message": {
                "content": "<command-name>/x</command-name>"
                "<command-args>hunter2-secret-value</command-args>"
            },
        }
    )
    assert "hunter2-secret-value" not in parse_line(line, redactor=redactor)[0].args
    out = json.dumps(
        {
            "type": "system",
            "subtype": "local_command",
            "content": "<local-command-stdout>hunter2-secret-value</local-command-stdout>",
        }
    )
    assert "hunter2-secret-value" not in parse_line(out, redactor=redactor)[0].stdout


# Acceptance-named aliases over the cases above.


def test_compact_summary_is_not_a_prompt():
    assert parse_line(LINES[4]) == []


def test_meta_entry_is_not_a_prompt():
    assert parse_line(LINES[5]) == []


def test_command_echo_parsed():
    test_command_echo_is_command_invoked()
    test_command_without_args_has_empty_args()


def test_local_command_output_parsed():
    test_system_local_command_is_output()
    test_user_local_command_stdout_is_output()
    test_stderr_body()


def test_compact_boundary_parsed():
    test_compact_boundary()
    test_compact_boundary_defaults()


def test_prompt_mentioning_tags_stays_prompt():
    test_ordinary_prompt_with_tag_mention_keeps_marker()


def test_other_system_lines_ignored():
    test_other_system_lines_yield_nothing()


def test_command_output_redacted():
    test_sidechain_flag_and_redaction()


async def test_tailer_delivers_command_events(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(FIXTURE.read_text())
    got = []

    async def sink(events):
        got.extend(events)

    await TranscriptTailer(path, sink, poll_interval=60).flush()
    assert [type(e).__name__ for e in got] == [
        "CommandInvoked",
        "CommandOutput",
        "CommandOutput",
        "CompactBoundary",
        "UserPrompt",
    ]
