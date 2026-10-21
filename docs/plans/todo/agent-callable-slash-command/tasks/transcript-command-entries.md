---
title: Transcript command entries
status: todo
repo: .
value: "Compact summaries and command echoes stop resurfacing old turn markers, so Stops after a compaction are not dropped as stale"
files: [src/claude_code_wrapper/transcript.py, tests/test_transcript_commands.py, tests/fixtures/transcripts/commands.jsonl]
verify: "uv run pytest tests/test_transcript_commands.py tests/test_transcript.py tests/test_router.py -q && mise run lint"
depends_on: []
owns: [transcript-command-events]
uses: []
model: sonnet
estimate: 60
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Transcript command entries

## Requirement

Transcript parser fix, always on (design D8). Compact summaries quote "All user messages", so today an old `<!-- ccw turn: ... -->` marker resurfaces as a UserPrompt and makes the next Stop look stale (C/transcript.py:146-158, 179-181). Owns part `transcript-command-events`.

- `parse_line` on a `user` line:
  - `isCompactSummary: true` or `isMeta: true` yields no UserPrompt and no marker;
  - a string content (or the joined text blocks) that starts, after leading whitespace, with `<command-name>` or `<command-message>` yields `CommandInvoked(name, args)` instead of a UserPrompt: `name` from `<command-name>` without the leading `/`, `args` from `<command-args>` stripped (empty string when absent);
  - content that starts with `<local-command-stdout>` or `<local-command-stderr>` yields `CommandOutput(stdout, stderr)` (each the tag body, empty string when that tag is absent);
  - everything else is unchanged: a real prompt that mentions the tags later in its text stays a UserPrompt with its marker; tool_result blocks and the interrupt and probe handling are untouched.
- `parse_line` on a `system` line (no `message` needed):
  - subtype `compact_boundary` yields `CompactBoundary(trigger, pre_tokens, post_tokens, duration_ms)` from `compactMetadata` (`trigger` default `"unknown"`, `pre_tokens` default 0, `post_tokens` and `duration_ms` None when absent);
  - subtype `local_command`, or any system line whose string content starts with `<local-command-`, yields `CommandOutput`;
  - every other system line still yields nothing.
- The new dataclasses (`CommandInvoked`, `CommandOutput`, `CompactBoundary`) derive from `_Event` (sidechain and agent_id flags attached as for the others) and join the `Event` union; the redactor applies to `args`, `stdout` and `stderr`.
- `TranscriptTailer` delivers them through its callback like the other events; `SessionRouter._on_transcript_events` ignores unknown event types today (its isinstance chain), so the router needs no change here.
- Fixture `tests/fixtures/transcripts/commands.jsonl` with lines in the 2.1.30x shapes from design.md "Facts": a command echo, local-command stdout as a system `local_command` line and as a user entry, a `compact_boundary` line, an `isCompactSummary` summary quoting `<!-- ccw turn: t_old -->`, an `isMeta` caveat line, and one ordinary prompt. Every test here parses from it or from inline lines of the same shape.

Interfaces: part `transcript-command-events` = `transcript.CommandInvoked{name, args}`, `transcript.CommandOutput{stdout, stderr}`, `transcript.CompactBoundary{trigger, pre_tokens, post_tokens, duration_ms}`.

## Acceptance conditions

- AC1: a compact summary quoting an old turn marker yields no UserPrompt and no marker [evidence: test:tests/test_transcript_commands.py::test_compact_summary_is_not_a_prompt]
- AC2: an isMeta user entry is not a prompt [evidence: test:tests/test_transcript_commands.py::test_meta_entry_is_not_a_prompt]
- AC3: a command echo yields CommandInvoked with name and args [evidence: test:tests/test_transcript_commands.py::test_command_echo_parsed]
- AC4: local-command stdout and stderr yield CommandOutput from a system line and from a user entry [evidence: test:tests/test_transcript_commands.py::test_local_command_output_parsed]
- AC5: a compact_boundary line yields CompactBoundary with its metadata and defaults [evidence: test:tests/test_transcript_commands.py::test_compact_boundary_parsed]
- AC6: a real prompt that mentions the tags later in its text stays a UserPrompt with its marker [evidence: test:tests/test_transcript_commands.py::test_prompt_mentioning_tags_stays_prompt]
- AC7: other system lines still yield nothing [evidence: test:tests/test_transcript_commands.py::test_other_system_lines_ignored]
- AC8: command args and output are redacted [evidence: test:tests/test_transcript_commands.py::test_command_output_redacted]
- AC9: the tailer delivers the new events to its callback [evidence: test:tests/test_transcript_commands.py::test_tailer_delivers_command_events]
- AC10: the existing transcript and router tests stay green [evidence: command:uv run pytest tests/test_transcript.py tests/test_router.py -q]

## Files

- `src/claude_code_wrapper/transcript.py`
- `tests/test_transcript_commands.py`
- `tests/fixtures/transcripts/commands.jsonl`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
