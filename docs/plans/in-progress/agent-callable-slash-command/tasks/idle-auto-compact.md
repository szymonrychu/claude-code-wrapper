---
title: Idle auto-compact
status: in-progress
repo: .
value: "Long-lived sessions compact themselves while idle, so the next turn starts with a small context"
files: [src/claude_code_wrapper/config.py, src/claude_code_wrapper/manager.py, src/claude_code_wrapper/router.py, README.md, tests/test_auto_compact.py]
verify: "uv run pytest tests/test_auto_compact.py tests/test_manager.py tests/test_exit_policy.py tests/test_config.py tests/test_readme_config.py -q && mise run lint"
depends_on: [ccw-mcp-injection-and-e2e, command-same-turn-resume]
owns: [auto-compact-config]
uses: [command-request-api, commands-config, fake-claude-commands]
model: sonnet
estimate: 75
actual: ""
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/agent-callable-slash-command--idle-auto-compact
branch: agent-callable-slash-command--idle-auto-compact
verified_head: ""
run: ""
provides: []
---

# Idle auto-compact

## Requirement
Optional compaction after a configured inactivity time (design D6; user decision). Owns part `auto-compact-config` (C/config.py).

- `config.py`: `AutoCompactConfig` at `sessions.auto_compact` (extra forbid): `idle_seconds: float = 0` (>= 0; 0 is off), `instructions: str = ""` (same one-line rule as compact params), `min_context_tokens: int = 0` (>= 0). Validator: `idle_seconds > 0` with `idle_timeout_seconds > 0` and `idle_seconds >= idle_timeout_seconds` is a ConfigError at `sessions.auto_compact.idle_seconds` (the reaper would stop the session first).
- Idle means: state ready, no turn, no command queued or running, and no event for `idle_seconds` other than the events of idle-origin commands (see below).
- Arming: armed when a turn ends (any terminal state); disarmed after firing, so it fires at most once per idle gap; the next turn end re-arms it. A session that never ran a turn is never auto-compacted. A turn or a client/agent command starting cancels the pending timer.
- Firing: when `min_context_tokens > 0` and the last main-chain context (input + cache_read + cache_creation tokens of the last non-sidechain Usage the router saw) is below it, nothing is queued (a debug log line, no event); otherwise `router.request_command("compact", {"instructions": ...} if instructions else {}, origin="idle")`, which runs on the idle path of task command-runner (session busy while it runs).
- Reaper (C/manager.py `_last_activity`): events of an idle-origin command (the command_* events whose record origin is idle, and every event emitted between its command_started and its command_completed or command_failed) do not count as activity, so an auto-compact never extends `idle_timeout_seconds`; the same rule applies to the idle clock of auto-compact itself.
- Exit policies are unaffected; shutdown during an auto compact abandons it (task command-runner-failures).
- README: config rows for `sessions.auto_compact.*` and a paragraph in `### sessions` on when it fires, the once-per-gap rule and the reaper interaction.

## Acceptance conditions

- AC1: auto_compact is off by default and nothing is typed after a turn [evidence: test:tests/test_auto_compact.py::test_auto_compact_off_by_default]
- AC2: after a turn and idle_seconds of quiet a compact with origin idle runs with the configured instructions [evidence: test:tests/test_auto_compact.py::test_auto_compact_fires_after_idle]
- AC3: it fires once per idle gap and again only after the next turn [evidence: test:tests/test_auto_compact.py::test_auto_compact_once_per_idle_gap]
- AC4: below min_context_tokens nothing is queued [evidence: test:tests/test_auto_compact.py::test_auto_compact_skipped_below_min_context]
- AC5: a session that never ran a turn is not auto-compacted [evidence: test:tests/test_auto_compact.py::test_auto_compact_not_armed_without_turn]
- AC6: a turn starting before idle_seconds cancels the pending auto compact [evidence: test:tests/test_auto_compact.py::test_turn_cancels_pending_auto_compact]
- AC7: an auto compact does not reset the idle reaper clock [evidence: test:tests/test_auto_compact.py::test_auto_compact_does_not_reset_reaper_clock]
- AC8: idle_seconds at or above idle_timeout_seconds is a config error [evidence: test:tests/test_auto_compact.py::test_idle_seconds_must_be_below_idle_timeout]
- AC9: README documents the new keys and existing manager and exit-policy tests stay green [evidence: command:uv run pytest tests/test_readme_config.py tests/test_manager.py tests/test_exit_policy.py tests/test_config.py -q]

## Files

- `src/claude_code_wrapper/config.py`
- `src/claude_code_wrapper/manager.py`
- `src/claude_code_wrapper/router.py`
- `README.md`
- `tests/test_auto_compact.py`

## Interfaces

`config.AutoCompactConfig` at `sessions.auto_compact`; `SessionRouter.context_tokens` (last main-chain context size).

## Expected outcome

With `sessions.auto_compact.idle_seconds` set, a session that went quiet after a turn compacts once with origin idle, below min_context_tokens it does not, and the idle reaper clock is unaffected.

## Steps

- Write tests/test_auto_compact.py (World, small idle_seconds, the fake's compact); run it and see it fail.
- Add AutoCompactConfig and its validator, the per-session timer and the reaper rule in manager.py, context_tokens tracking in router.py, the README rows.
- Run `uv run pytest tests/test_auto_compact.py tests/test_manager.py tests/test_exit_policy.py tests/test_config.py tests/test_readme_config.py -q && mise run lint`.
- A retirement test, if any, asserts on `git ls-files <path>`, never on the filesystem.

## Notes

A timer per session (asyncio, cancelled on close) or the manager's reaper loop are both fine; tests use a small idle_seconds (for example 0.3) and the fake's compact.
