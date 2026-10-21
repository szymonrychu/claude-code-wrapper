---
title: Fake claude slash commands
status: todo
repo: .
value: "Every command test runs against a fake that compacts, reloads and fails like real claude, with no API spend"
files: [tests/fake_claude/fake_claude.py, tests/test_fake_claude_commands.py]
verify: "uv run pytest tests/test_fake_claude_commands.py tests/test_fake_claude.py -q && mise run lint"
depends_on: []
owns: [fake-claude-commands]
uses: []
model: sonnet
estimate: 90
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Fake claude slash commands

## Requirement
Teach the fake claude (`tests/fake_claude/fake_claude.py`) Claude Code's local slash commands, so every later task tests the command lifecycle against it (design D4, D7, D8; the transcript and hook shapes are in design.md "Facts"). Owns part `fake-claude-commands`. This task does not edit `tests/conftest.py`.

- Interactive input whose submitted text (stripped) starts with `/` plus a known name runs a local command instead of a turn: no UserPromptSubmit, no Stop hook, no assistant line, the idle footer after it. Known names: `compact`, `reload-plugins`, `reload-skills`, `mcp`, `context`. An unknown `/name` prints `Unknown command: /name` and returns to idle with no transcript line.
- Every known command first writes a user transcript entry whose string content is `<command-name>/NAME</command-name>\n<command-message>NAME</command-message>\n<command-args>ARGS</command-args>` (ARGS is the rest of the line, stripped; empty allowed).
- `/compact [instructions]`, outcome `ok` (default): run PreCompact hooks with `{trigger: "manual", custom_instructions: ARGS or null}` plus the usual hook fields (session_id, transcript_path, cwd, hook_event_name); a PreCompact block (exit 2, or stdout `{"decision": "block"}`) ends it as `canceled`. Then write a `{"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted", "compactMetadata": {"trigger": "manual", "preTokens": N, "postTokens": M, "durationMs": D}}` line with the same sessionId, then a user entry with `isCompactSummary: true` and `isVisibleInTranscriptOnly: true` whose content starts with "This session is being continued from a previous conversation" and contains "All user messages:" followed by every earlier user prompt of this process verbatim (turn markers included, so the stale-marker case is real), then run PostCompact hooks with `{trigger: "manual", compact_summary: <summary text>}`, print `Compacted` on the PTY and return to idle.
- Scenario key `"commands": {"<name>": {...}}` (top level) selects outcomes. compact: `outcome` one of `ok`, `canceled` ("Compaction canceled."), `not_enough` ("Not enough messages to compact."), `error` ("Error compacting conversation"), `hang` (nothing until ESC; ESC then behaves as `canceled`); `post_compact_hook: false` skips the PostCompact hook (boundary-only signal); `delay_seconds` before completing. A failure outcome writes `{"type": "system", "subtype": "local_command", "content": "<local-command-stderr>MSG</local-command-stderr>"}`, prints MSG, writes no boundary and runs no PostCompact.
- `/reload-plugins [--force]`: writes a system `local_command` line with `<local-command-stdout>STDOUT</local-command-stdout>`; default STDOUT `Reloaded: 0 plugins, 0 skills, 0 agents, 0 hooks, 0 MCP servers`; scenario `stdout` overrides it (for example a text containing `Run /reload-plugins --force to apply.`); `as_user: true` writes the same content as a user entry instead of a system line (both shapes occur); `hang: true` writes nothing until ESC.
- `/reload-skills`, `/context`, `/mcp ARGS`: the same stdout mechanism, defaults `Reloaded skills`, `Context usage: 0 tokens`, `Reconnected to all MCP servers` (`/mcp reconnect NAME` -> `Reconnected to NAME`), scenario `stdout` and `hang` override.
- Every submitted input line is appended to `$CLAUDE_CONFIG_DIR/fake-inputs.jsonl` as `{"text", "state": "idle"|"busy", "ts"}`. Bytes other than ESC that arrive while a turn runs are recorded with state `busy` (and otherwise dropped as today), so tests can prove ccw never typed into a running turn.
- Turn step `{"auto_compact": {"pre_tokens": N}}`: inside a turn, runs PreCompact and PostCompact with `trigger: "auto"` and writes the boundary (trigger `auto`) and summary lines, then continues the steps (models Claude Code's own compaction).
- The module docstring documents every new step, scenario key and file (it is the fake's spec).
- Existing tests/test_fake_claude.py stays green.

## Acceptance conditions

- AC1: /compact writes the command entry, the compact_boundary line, the summary and runs PostCompact [evidence: test:tests/test_fake_claude_commands.py::test_compact_writes_boundary_and_post_compact]
- AC2: the compact summary quotes every earlier prompt verbatim with its turn marker [evidence: test:tests/test_fake_claude_commands.py::test_compact_summary_quotes_prior_prompts]
- AC3: PreCompact receives trigger manual and the instructions, and a PreCompact block cancels [evidence: test:tests/test_fake_claude_commands.py::test_pre_compact_payload_and_block]
- AC4: canceled, not_enough and error outcomes write the stderr line and no boundary [evidence: test:tests/test_fake_claude_commands.py::test_compact_failure_outcomes]
- AC5: a hanging compact completes as canceled after ESC [evidence: test:tests/test_fake_claude_commands.py::test_compact_hang_cancelled_by_esc]
- AC6: post_compact_hook false writes the boundary without running PostCompact [evidence: test:tests/test_fake_claude_commands.py::test_compact_without_post_compact_hook]
- AC7: /reload-plugins writes default and overridden stdout, as a system line or a user entry [evidence: test:tests/test_fake_claude_commands.py::test_reload_plugins_stdout_and_force_hint]
- AC8: /reload-skills, /context and /mcp reconnect write their stdout [evidence: test:tests/test_fake_claude_commands.py::test_other_commands_write_local_stdout]
- AC9: a slash command fires neither UserPromptSubmit nor Stop and an unknown one writes nothing [evidence: test:tests/test_fake_claude_commands.py::test_slash_command_fires_no_stop]
- AC10: input arriving during a turn is recorded with state busy in fake-inputs.jsonl [evidence: test:tests/test_fake_claude_commands.py::test_input_while_busy_recorded]
- AC11: the auto_compact step writes an auto boundary and runs both compact hooks with trigger auto [evidence: test:tests/test_fake_claude_commands.py::test_auto_compact_step]
- AC12: the existing fake claude tests stay green [evidence: command:uv run pytest tests/test_fake_claude.py -q]

## Files

- `tests/fake_claude/fake_claude.py`
- `tests/test_fake_claude_commands.py`

## Interfaces

part `fake-claude-commands` = the scenario `commands` key, the `auto_compact` step, `fake-inputs.jsonl`, and the transcript and hook shapes above.

## Expected outcome

Typing `/compact`, `/reload-plugins`, `/reload-skills`, `/mcp reconnect` or `/context` into the fake under a PTY produces the same transcript entries, hook calls and failure modes real claude 2.1.30x produces, selectable per scenario, and every input is logged with the fake's busy or idle state.

## Steps

- Write tests/test_fake_claude_commands.py (fake under a PTY via the existing helpers of tests/test_fake_claude.py, recording hooks from tests/fixtures/hooks/record_hook.sh in settings.json); run `uv run pytest tests/test_fake_claude_commands.py -q` and see every test fail.
- Implement slash-command dispatch, the compact flow and its outcomes, the stdout commands, `fake-inputs.jsonl` and the `auto_compact` step in tests/fake_claude/fake_claude.py; update the module docstring.
- Run `uv run pytest tests/test_fake_claude_commands.py tests/test_fake_claude.py -q && mise run lint`; expect all passed and lint clean.
- A retirement test, if any, asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Hooks run as today (`run_hooks` with settings.json `hooks.<Event>`); PreCompact and PostCompact hook groups only exist once ccw installs relays for them (task command-completion-signals), so the tests here install recording hooks directly in settings.json (tests/fixtures/hooks/record_hook.sh). Order of the boundary and summary lines follows real 2.1.30x transcripts: boundary first.
