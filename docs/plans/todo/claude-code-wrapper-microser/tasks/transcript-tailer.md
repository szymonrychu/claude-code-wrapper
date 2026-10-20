---
title: Transcript tailer
status: todo
repo: claude-code-wrapper
value: "Assistant text, tool use and usage stream out of claude transcripts"
files: [src/claude_code_wrapper/transcript.py, tests/test_transcript.py, tests/fixtures/transcripts/basic.jsonl, tests/fixtures/transcripts/subagent.jsonl]
verify: "uv run pytest tests/test_transcript.py -q && mise run lint"
depends_on: [claude-env-model, fake-claude]
owns: [transcript-tailer]
uses: [redactor, fake-claude]
model: ""
estimate: 90
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Transcript tailer

## Requirement

Build the Claude transcript reader (D4, D7, D13) in `src/claude_code_wrapper/transcript.py`.

- `PROJECT_DIR_NAME = "workdir"`; `transcript_path(config_dir, session_id, project_dir_name=PROJECT_DIR_NAME) -> Path` = `<config_dir>/projects/<name>/<session_id>.jsonl`; `mangle_cwd(path) -> str` (every non-alphanumeric char -> `-`; used only as a fallback name).
- Typed events (dataclasses), each with `sidechain: bool` and `agent_id: str | None`: `UserPrompt(text, turn_marker)` where `turn_marker` is the id parsed from `<!-- ccw turn: turn-... -->`; `AssistantText(text, message_id, model)`; `Thinking(text, message_id)`; `ToolUse(id, name, input)`; `ToolResult(tool_use_id, is_error, text)` (text flattened from string or block content, truncated to 4000 chars with a `...[truncated]` suffix); `Usage(message_id, model, input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens)`; `Interrupted()` for user content starting with `[Request interrupted by user`; `ProbeMarker(probe_id)` for `<!-- ccw probe: ... -->`.
- `parse_line(line, *, redactor=None, sidechain=False, agent_id=None) -> list[event]`: handles `user` and `assistant` lines (string content or block lists); other types (attachment, system, queue-operation, file-history-snapshot, summary, ...) yield nothing; malformed JSON yields nothing (debug log). Text fields pass through the redactor.
- `UsageTotals`: `add(Usage)` dedupes by message_id (claude repeats usage per content block of one message) and sums per model; `reset()`; `as_dict()` `{model: {input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens}}`; `last_usage`.
- `TranscriptTailer(path, on_events, *, poll_interval=0.2, redactor=None, sidechain=False, agent_id=None)`: `async run()` waits for the file to appear, then follows appended lines; buffers a partial trailing line (discarding a line longer than 16 MiB with a warning); reopens from the start when the inode changes or the file shrinks; `switch(path)` follows another file from its start; `stop()`. `on_events(list)` is called per batch.
- `SubagentWatcher(session_transcript_path, on_events, poll_interval=1.0, redactor=None)`: polls `<transcript dir>/<session id>/subagents/agent-*.jsonl` and starts one sidechain tailer per new file with `agent_id` from the file name; `stop()` stops all.
- Fixtures: `tests/fixtures/transcripts/basic.jsonl` and `tests/fixtures/transcripts/subagent.jsonl` built from REAL Claude Code lines (copied and secret-scrubbed from the tatara and magic test data), plus a test that the fake claude's lines parse to the same event kinds.

## Acceptance conditions

- AC1: Assistant text and tool_use blocks parse into typed events [evidence: test:tests/test_transcript.py::test_parse_assistant_text_and_tool_use]
- AC2: Error tool results are flagged and long text truncated [evidence: test:tests/test_transcript.py::test_parse_tool_result_error_truncated]
- AC3: Usage is deduped by message id and summed per model [evidence: test:tests/test_transcript.py::test_usage_deduped_by_message_id]
- AC4: The ccw turn marker is extracted from user prompts [evidence: test:tests/test_transcript.py::test_turn_marker_extracted]
- AC5: The interrupt line is detected [evidence: test:tests/test_transcript.py::test_interrupt_line_detected]
- AC6: Malformed and unknown lines are skipped [evidence: test:tests/test_transcript.py::test_malformed_line_skipped]
- AC7: The tailer follows appends and buffers partial lines [evidence: test:tests/test_transcript.py::test_tailer_follows_appends_and_partial_lines]
- AC8: The tailer waits for a file that does not exist yet [evidence: test:tests/test_transcript.py::test_tailer_waits_for_file]
- AC9: The tailer reopens on truncation or inode change [evidence: test:tests/test_transcript.py::test_tailer_reopens_on_truncate]
- AC10: The subagent watcher tails agent files as sidechain with agent ids [evidence: test:tests/test_transcript.py::test_subagent_watcher_tags_sidechain]
- AC11: Text fields are redacted [evidence: test:tests/test_transcript.py::test_text_redacted]
- AC12: Real Claude Code fixture lines parse [evidence: test:tests/test_transcript.py::test_real_fixture_lines_parse]
- AC13: Lines written by the fake claude parse to the same event kinds [evidence: test:tests/test_transcript.py::test_fake_claude_lines_parse]
- AC14: transcript_path uses the pinned project dir name [evidence: test:tests/test_transcript.py::test_transcript_path_pinned_dir]

## Files

- `src/claude_code_wrapper/transcript.py`
- `tests/test_transcript.py`
- `tests/fixtures/transcripts/basic.jsonl`
- `tests/fixtures/transcripts/subagent.jsonl`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
