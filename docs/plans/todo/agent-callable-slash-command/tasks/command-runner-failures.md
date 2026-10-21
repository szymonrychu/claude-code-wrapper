---
title: Command runner failures
status: todo
repo: .
value: "A crash, destroy, shutdown or interrupt during a command never leaves a session stuck busy or typing"
files: [src/claude_code_wrapper/router.py, src/claude_code_wrapper/session.py, src/claude_code_wrapper/manager.py, tests/test_command_runner_failures.py]
verify: "uv run pytest tests/test_command_runner_failures.py tests/test_command_runner.py tests/test_turn_continuity.py tests/test_destroy_io.py tests/test_manager.py -q && mise run lint"
depends_on: [command-runner]
owns: []
uses: [command-request-api, fake-claude-commands]
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

# Command runner failures

## Requirement
Failure handling of the command runner on an idle session (design D4 step 6 for the idle path, D6 "shutdown abandons a running command"). The in-turn variants are task command-same-turn-resume.

- Crash: claude exiting while a command runs fails that command with error `claude_exited` (`command_failed`) and cancels the queued ones (state cancelled, `command_failed` each); the session then follows its restart policy as today; after a successful relaunch the session is ready and accepts turns and new commands.
- Destroy and shutdown: `DELETE /v1/sessions/{id}` or service shutdown while a command runs fails it with error `session_stopped` and cancels the queued ones; the runner task ends within the router's close drain (`CLOSE_DRAIN_SECONDS`), nothing is typed after close starts, and `session_closed` is still emitted exactly once.
- Races: a client turn submitted while an idle command runs gets 409 busy and no prompt is pasted; a turn submitted the moment the runner drains (state back to ready) is accepted normally; a command requested while the session is booting or dead is rejected with `CommandRejected("invalid_state")` (the endpoint maps it to 409).
- Interrupt on an idle session while a command runs (`POST /interrupt`): ESC to claude, the command fails with error `interrupted`, queued ones cancelled, the session returns to ready.
- The runner never leaves the session busy: every exit path (completion, failure, veto, timeout, crash, interrupt, close) runs `Session.end_command()` or the stop path.

## Acceptance conditions

- AC1: claude dying during an idle command fails it as claude_exited, cancels the queued ones and the relaunched session accepts turns [evidence: test:tests/test_command_runner_failures.py::test_crash_during_command_fails_claude_exited]
- AC2: destroying the session during a command fails it as session_stopped and emits session_closed once [evidence: test:tests/test_command_runner_failures.py::test_destroy_abandons_running_command]
- AC3: service shutdown during a command ends the runner within the close drain [evidence: test:tests/test_command_runner_failures.py::test_shutdown_abandons_running_command]
- AC4: a client turn submitted during an idle command gets 409 and nothing is pasted [evidence: test:tests/test_command_runner_failures.py::test_turn_during_idle_command_is_busy]
- AC5: a command requested while the session is booting or dead is rejected as invalid_state [evidence: test:tests/test_command_runner_failures.py::test_command_rejected_when_not_running]
- AC6: an interrupt during an idle command fails it as interrupted and the session returns to ready [evidence: test:tests/test_command_runner_failures.py::test_interrupt_during_idle_command]
- AC7: existing session, continuity and destroy tests stay green [evidence: command:uv run pytest tests/test_command_runner.py tests/test_turn_continuity.py tests/test_destroy_io.py tests/test_manager.py -q]

## Files

- `src/claude_code_wrapper/router.py`
- `src/claude_code_wrapper/session.py`
- `src/claude_code_wrapper/manager.py`
- `tests/test_command_runner_failures.py`

## Interfaces

`CommandRejected("invalid_state")` from `SessionRouter.request_command`; record errors `claude_exited`, `session_stopped`, `interrupted`.

## Expected outcome

Every way a command can be cut short on an idle session (crash, destroy, shutdown, interrupt, a racing turn, a session that is not running) ends with a terminal record, a ready or stopped session and nothing typed afterwards.

## Steps

- Write tests/test_command_runner_failures.py using the fake's hang outcomes to hold a command running; run it and see it fail.
- Handle the exit, close, interrupt and state paths in router.py, session.py and manager.py.
- Run `uv run pytest tests/test_command_runner_failures.py tests/test_command_runner.py tests/test_turn_continuity.py tests/test_destroy_io.py tests/test_manager.py -q && mise run lint`.
- A retirement test, if any, asserts on `git ls-files <path>`, never on the filesystem.

## Notes

The fake's `hang` outcomes keep a command running so the test can crash, destroy or interrupt mid-command; `{"exit": code}` is not reachable from a slash command, so crash the fake by killing its pid (`session.pty`), as tests/test_turn_continuity.py does.
