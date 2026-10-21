---
title: Command same-turn resume
status: in-progress
repo: .
value: "The agent keeps working in the same turn after its own /compact or /reload-plugins, with one turn record"
files: [src/claude_code_wrapper/router.py, src/claude_code_wrapper/session.py, README.md, tests/test_command_turn_resume.py]
verify: "uv run pytest tests/test_command_turn_resume.py tests/test_stop_gate.py tests/test_turn_continuity.py tests/test_session_turns.py tests/test_command_runner.py tests/test_command_runner_failures.py tests/test_readme_api.py -q && mise run lint"
depends_on: [command-runner-failures]
owns: [continue-turn]
uses: [command-request-api, command-events, fake-claude-commands]
model: opus
estimate: 90
actual: ""
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/agent-callable-slash-command--command-same-turn-resume
branch: agent-callable-slash-command--command-same-turn-resume
verified_head: ""
run: ""
provides: []
---

# Command same-turn resume

## Requirement
Same-turn lifecycle: a command queued during a turn runs at that turn's Stop and the agent resumes in the same turn (design D4 steps 2, 4, 5, 6, 7; user decision). Owns part `continue-turn` (C/session.py).

- Stop gate (C/router.py `_stop_gate`): it also gates when the queue holds commands for the running turn, with no onStop hook and no `defer_stop_on_background_tasks` configured. Order on a Stop that targets the running turn: onStop fires first; a block wins (`turn_continued`, the commands stay queued); a hook defer or the background-task defer wins next (`turn_deferred` as today, commands stay queued and run at the later Stop that is not deferred); otherwise answer allow, emit `turn_deferred {turn_id, reason: "command", commands: [ids]}` and start the runner only after the relay response has been returned to claude (spawned after the handler returns, never awaited inside it). The existing `turn_deferred` gains `reason: "hook"` or `"background_tasks"`.
- Runner in a turn: the record stays the running turn (state busy, same turn_id); each command runs as in task command-runner (quiet screen, onCommand, type, wait, events, metrics).
- Continue (D4.4): when the queue is drained, `Session.continue_turn(record, text)` (factored out of `_continue_held`, C/session.py:648-669, which now calls it) pastes `commands_cfg.continue_prompt` plus a results line, `Command results: <name> <state>[ <result or error>]; ...`, with the same turn marker; emit `turn_resumed {turn_id, cause: "command", commands: [ids]}` (the restart resume of K12 gets `cause: "restart"`); the next Stop completes the same record; usage keeps accumulating across the command; no second `turn_started`.
- After /compact the summary quotes old markers; with the parser fix of task transcript-command-entries the Stop after the continuation completes the turn (it is not stale).
- Interrupt (D4.5): queued commands of the turn are cancelled; a running one gets ESC and fails with error `interrupted`; the turn ends interrupted; no continuation is pasted.
- Crash (D4.6): a command running when claude dies fails `claude_exited`, queued ones are cancelled, and the K12 policy decides the turn (`restart_continue_prompt` set: the held turn continues with it; empty: the turn fails `claude_exited`).
- turn_max_seconds (D4.7) keeps counting through the command: a turn hitting the limit during a command ends `timeout`, the running command gets ESC and fails `timeout`, queued ones are cancelled, no continuation.
- A command requested by the client during a turn takes the same path as an agent one.
- README: `### turn_deferred` documents `reason` and `commands`, `### turn_resumed` documents `cause`, `### onStop` says pending commands defer the Stop after the hook allows.

## Acceptance conditions

- AC1: a command queued during a turn runs at its Stop and the same turn resumes and completes once [evidence: test:tests/test_command_turn_resume.py::test_command_runs_at_stop_and_turn_resumes]
- AC2: the runner types only after the Stop relay answer was returned [evidence: test:tests/test_command_turn_resume.py::test_runner_starts_after_relay_answer]
- AC3: an onStop block keeps the command queued and it runs at the next allowed Stop [evidence: test:tests/test_command_turn_resume.py::test_onstop_block_keeps_command_queued]
- AC4: a background-task defer keeps the command queued until the final Stop [evidence: test:tests/test_command_turn_resume.py::test_background_defer_keeps_command_queued]
- AC5: the continuation carries the continue prompt, the results line and the same turn marker [evidence: test:tests/test_command_turn_resume.py::test_continue_prompt_lists_results_with_same_marker]
- AC6: the Stop after a compact whose summary quotes old markers completes the turn [evidence: test:tests/test_command_turn_resume.py::test_stop_after_compact_summary_completes_turn]
- AC7: an interrupt cancels queued commands, ESCs the running one and ends the turn without continuation [evidence: test:tests/test_command_turn_resume.py::test_interrupt_cancels_queued_and_running]
- AC8: a crash mid-command fails it as claude_exited and the K12 policy decides the turn [evidence: test:tests/test_command_turn_resume.py::test_crash_mid_command_follows_k12]
- AC9: turn_max_seconds ends a turn whose command is still running [evidence: test:tests/test_command_turn_resume.py::test_turn_max_seconds_counts_through_command]
- AC10: usage accumulates across the command into the one turn record [evidence: test:tests/test_command_turn_resume.py::test_usage_accumulates_across_command]
- AC11: turn_deferred carries reason and turn_resumed carries cause for every path [evidence: test:tests/test_command_turn_resume.py::test_deferred_reason_and_resumed_cause]
- AC12: existing stop gate, continuity and turn tests stay green [evidence: command:uv run pytest tests/test_stop_gate.py tests/test_turn_continuity.py tests/test_session_turns.py tests/test_command_runner.py tests/test_command_runner_failures.py tests/test_readme_api.py -q]

## Files

- `src/claude_code_wrapper/router.py`
- `src/claude_code_wrapper/session.py`
- `README.md`
- `tests/test_command_turn_resume.py`

## Interfaces

`Session.continue_turn(record, text)`; `turn_deferred.reason`, `turn_resumed.cause`.

## Expected outcome

A command queued during a turn runs after that turn's Stop is answered, the continue prompt with the same marker resumes the agent, and one turn record covers the whole span, including the interrupt, crash and time-limit paths.

## Steps

- Write tests/test_command_turn_resume.py (scenario turns that end after a client request_command plus a continuation turn matching the continue prompt); run it and see it fail.
- Extend _stop_gate, factor Session.continue_turn out of _continue_held, wire the runner's end to the continuation, add the reason and cause fields and the README notes.
- Run `uv run pytest tests/test_command_turn_resume.py tests/test_stop_gate.py tests/test_turn_continuity.py tests/test_session_turns.py tests/test_command_runner.py tests/test_command_runner_failures.py tests/test_readme_api.py -q && mise run lint`.
- A retirement test, if any, asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Tests drive it with the fake: a turn whose steps end normally after a client `request_command` (or with the `mcp` step once task ccw-mcp-injection-and-e2e lands; not needed here), and a second scenario turn whose regex matches the continue prompt. Use `sessions.commands.quiet_seconds` small.
