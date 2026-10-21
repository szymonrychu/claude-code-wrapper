"""Command echoes, compact boundaries and summaries in the transcript (design D8)."""

import json
from pathlib import Path

from claude_code_wrapper.redact import Redactor
from claude_code_wrapper.transcript import (
    CommandInvoked,
    CommandOutput,
    CompactBoundary,
    UserPrompt,
    parse_line,
)

FIXTURE = Path(__file__).parent / "fixtures" / "transcripts" / "commands.jsonl"
LINES = FIXTURE.read_text().splitlines()


def test_command_echo_is_command_invoked():
    assert parse_line(LINES[0]) == [CommandInvoked(name="compact", args="keep the plan")]


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
    events = parse_line(line)
    assert events and all(isinstance(e, CommandInvoked) for e in events)


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
                "content": "<command-name>/x</command-name><command-args>hunter2-secret-value</command-args>"
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
