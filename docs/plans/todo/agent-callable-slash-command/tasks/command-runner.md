---
title: Command runner
status: todo
repo: .
value: "Queued slash commands actually run in the session's claude, safely at an idle prompt, with results and hooks"
files: [src/claude_code_wrapper/router.py, src/claude_code_wrapper/session.py, src/claude_code_wrapper/commands.py, tests/test_command_runner.py]
verify: "uv run pytest tests/test_command_runner.py tests/test_router.py tests/test_session_turns.py tests/test_stop_gate.py tests/test_turn_continuity.py -q && mise run lint"
depends_on: [command-completion-signals]
owns: [command-request-api]
uses: [command-queue, commands-config, command-signals, compact-relay-events, command-events, command-hooks, command-metrics, fake-claude-commands]
model: opus
estimate: 90
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Command runner

## Requirement
The command request API and the runner, on an idle session (design D3 request, D4 steps 1, 3 and 8, D7 emission). The turn integration is task command-same-turn-resume; crash, shutdown and race handling is task command-runner-failures. Owns part `command-request-api` (C/router.py).

- `SessionRouter.request_command(name, params=None, *, origin) -> CommandRecord`: `validate_params` with `commands_cfg.allow_force`, then `self.commands.request(...)` with `turn_id` = the running turn's id when busy, else None. `CommandRejected` propagates to the caller unchanged. A new record emits `command_queued {record}`; a coalesced duplicate returns the existing record and emits nothing. When the session is ready with no turn and the runner is idle, the runner starts at once (idle path); when a turn is running the command stays queued (the Stop gate of task command-same-turn-resume starts it).
- Idle path (D4.8): `Session.begin_command()` moves ready to busy without a turn record (public state busy), so `POST /v1/sessions/{id}/turns` gets 409 busy and the idle reaper (ready only) leaves the session alone; `Session.end_command()` returns it to ready once the queue is drained. Commands queued during a run run in order, one at a time; there is never more than one runner per session.
- Runner, per command:
  1. wait for a quiet screen: no PTY output for `commands_cfg.quiet_seconds` (Session keeps the time of the last output);
  2. fire `onCommand` (blocking) with `{command_id, name, args, origin, turn_id}`; a deny fails the record with error `vetoed` (output = the reason) and emits `command_failed`;
  3. mark the record running, emit `command_started {record}`, take `signals.mark()`;
  4. type `commands.render(name, args)` through the PTY's paste-then-CR path with no turn marker (`Session.send_command(line)`; the single writer of C/pty_proc.py:223);
  5. wait, up to `timeout_seconds[name]` or `timeout_seconds["default"]`:
     - compact: a `post_compact` or `compact_boundary` signal with trigger `manual` -> completed, result `ok`; a `command_output` whose text contains `Compaction canceled.`, `Not enough messages to compact.` or `Error compacting conversation` -> failed, error `compact_failed`, output = that text;
     - other commands: the first `command_output` after the matching `command_invoked` -> completed with output = stdout (cut to 4000 characters); for reload-plugins an output containing `--force to apply` -> completed with result `skipped_cache`; an output with only stderr -> failed, error `command_error`;
     - timeout: send ESC once, failed, error `timeout`;
  6. emit `command_completed {record}` or `command_failed {record}`, count `Metrics.command_finished(name, origin, state, seconds)`, fire `onCommandComplete` with the record, then the next queued command.
- Never type while a turn is running or while another write is in flight: in the fake's `fake-inputs.jsonl` every command line has state idle.

## Acceptance conditions

- AC1: a compact requested on an idle session runs, completes with result ok and emits queued, started, compacted and completed in order [evidence: test:tests/test_command_runner.py::test_idle_compact_runs_and_completes]
- AC2: canceled, not_enough and error compact outcomes fail with compact_failed and the message as output [evidence: test:tests/test_command_runner.py::test_compact_failure_reported]
- AC3: a compact whose only signal is the boundary line completes [evidence: test:tests/test_command_runner.py::test_compact_boundary_only_completes]
- AC4: reload-plugins completes with its stdout, and with result skipped_cache on the force hint [evidence: test:tests/test_command_runner.py::test_reload_plugins_output_and_skipped_cache]
- AC5: reload-skills, mcp reconnect and context complete with their output [evidence: test:tests/test_command_runner.py::test_other_commands_complete_with_output]
- AC6: a command that never completes is failed as timeout after one ESC [evidence: test:tests/test_command_runner.py::test_command_timeout_sends_esc]
- AC7: an onCommand deny fails the command as vetoed and nothing is typed [evidence: test:tests/test_command_runner.py::test_on_command_deny_vetoes]
- AC8: onCommandComplete receives the final record [evidence: test:tests/test_command_runner.py::test_on_command_complete_fired_with_record]
- AC9: commands queued together run one at a time in order and a duplicate is coalesced [evidence: test:tests/test_command_runner.py::test_commands_run_in_sequence]
- AC10: an idle command makes the session busy so a client turn gets 409, then returns it to ready [evidence: test:tests/test_command_runner.py::test_idle_command_marks_busy_and_turn_409]
- AC11: every typed command line was received while the fake was idle [evidence: test:tests/test_command_runner.py::test_never_typed_while_busy]
- AC12: ccw_commands_total and ccw_command_duration_seconds count each finished command [evidence: test:tests/test_command_runner.py::test_command_metrics_counted]
- AC13: existing router, session and turn tests stay green [evidence: command:uv run pytest tests/test_router.py tests/test_session_turns.py tests/test_stop_gate.py tests/test_turn_continuity.py -q]

## Files

- `src/claude_code_wrapper/router.py`
- `src/claude_code_wrapper/session.py`
- `src/claude_code_wrapper/commands.py`
- `tests/test_command_runner.py`

## Interfaces

`SessionRouter.request_command(name, params=None, *, origin) -> CommandRecord`; `Session.begin_command()`, `Session.end_command()`, `Session.send_command(line)`, `Session.last_output_at`.

## Expected outcome

On an idle session `request_command` types the slash command at a quiet prompt, detects its completion or failure, emits the events, fires the hooks, counts the metrics and runs queued commands one by one, with the session busy meanwhile.

## Steps

- Write tests/test_command_runner.py against the World and the fake's `commands` scenario key (small quiet_seconds and timeouts); run it and see it fail.
- Implement request_command and the runner in router.py and begin_command, end_command, send_command and the last-output clock in session.py; extend commands.py only where the runner needs it.
- Run `uv run pytest tests/test_command_runner.py tests/test_router.py tests/test_session_turns.py tests/test_stop_gate.py tests/test_turn_continuity.py -q && mise run lint`.
- A retirement test, if any, asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Reuses `commands.CommandQueue`, `commands.render`, `SignalBus` (task command-completion-signals), the command events, hooks and metrics (task command-event-vocabulary) and the fake's `commands` scenario key (task fake-claude-slash-commands). Tests run the World with `sessions.commands.quiet_seconds` small (for example 0.2) and short timeouts.
