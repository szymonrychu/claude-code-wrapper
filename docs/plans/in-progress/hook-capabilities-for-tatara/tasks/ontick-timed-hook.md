---
title: onTick timed hook
status: done
repo: .
value: "Periodic hook actions for safety pushes and metrics pushes without sidecars (K7)"
files: [src/claude_code_wrapper/config.py, src/claude_code_wrapper/hooks.py, src/claude_code_wrapper/router.py, README.md, tests/test_on_tick.py]
verify: "uv run pytest tests/test_on_tick.py tests/test_config.py tests/test_router.py tests/test_readme_api.py tests/test_readme_config.py -q && mise run lint"
depends_on: [turn-metadata-on-submit]
owns: [on-tick-event]
uses: [ccw-world-fixture]
model: sonnet
estimate: 75
actual: 5
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/hook-capabilities-for-tatara--ontick-timed-hook
branch: hook-capabilities-for-tatara--ontick-timed-hook
verified_head: bfd57c9cdc706e2dc604ee43f87dc14c53e712ac
run: hook-capabilities-for-tatara
provides: [dispatcher-fire-only-index, session-turn-elapsed]
acs_for_audit: []
process_gaps: []
---

# onTick timed hook

## Requirement

K7 onTick timed hook (design D11). Owns part `on-tick-event`.

- `onTick` joins `HOOK_EVENTS` and `HooksConfig`. Hook actions gain `interval_seconds: float | None` (> 0) and `when: Literal["busy", "always"] | None`. On onTick `interval_seconds` is required and `when` defaults to `busy`; on every other event both are rejected. Errors point at `hooks.onTick.<i>.<type>.interval_seconds` like the matcher validator.
- The router owns one timer task per onTick action, started when the session first reaches ready, kept across claude restarts and cancelled in `close()`. Each tick fires only that action (`HookDispatcher.fire` gains a way to run a single action index). `when: busy` skips ticks with no running turn. A tick whose previous run of the same action has not finished is skipped (no overlap).
- data: `turn_id` (null when idle), `elapsed_seconds` of the running turn (null when idle), `interval_seconds`.
- `EVENT_DECISIONS` (from hook-result-fields, existing events only) gains `onTick` = `allow` only, added by this task.
- onTick decisions, annotations and outputs are ignored (it cannot veto). onTick is not relayed from claude and is not in `_GATED_EVENTS`.
- README: `### \`onTick\`` under `## Hooks` and the two action keys in the hooks configuration reference.

## Acceptance conditions

- AC1: interval_seconds is required on onTick [evidence: test:tests/test_on_tick.py::test_interval_required_on_on_tick]
- AC2: interval_seconds and when are rejected on other events [evidence: test:tests/test_on_tick.py::test_interval_and_when_rejected_on_other_events]
- AC3: a busy tick fires only while a turn runs [evidence: test:tests/test_on_tick.py::test_busy_tick_fires_only_during_turn]
- AC4: an always tick fires while idle [evidence: test:tests/test_on_tick.py::test_always_tick_fires_when_idle]
- AC5: a slow tick action never overlaps itself [evidence: test:tests/test_on_tick.py::test_tick_does_not_overlap_itself]
- AC6: ticks stop when the router closes [evidence: test:tests/test_on_tick.py::test_ticks_stop_on_close]
- AC7: tick data has turn_id, elapsed_seconds and interval_seconds [evidence: test:tests/test_on_tick.py::test_tick_data_fields]
- AC8: README documents onTick [evidence: command:uv run pytest tests/test_readme_api.py tests/test_readme_config.py -q]

## Files

- `src/claude_code_wrapper/config.py`
- `src/claude_code_wrapper/hooks.py`
- `src/claude_code_wrapper/router.py`
- `README.md`
- `tests/test_on_tick.py`

## Interfaces

- Part `on-tick-event`: `config.py::HOOK_EVENTS` (onTick), action fields `interval_seconds`, `when`; `HookDispatcher.fire(..., only_index=...)` (or equivalent); router tick tasks.

## Expected outcome

Operators can run a hook action every N seconds while a turn runs (or always), without sidecars, and ticks stop with the session.

## Steps

- Write `tests/test_on_tick.py` (config validation unit tests; `world` sessions with short intervals and a sleeping fake turn); see it fail.
- Config fields and validator; single-action fire; router timers started at first ready, cancelled in `close()`.
- README.
- `uv run pytest tests/test_on_tick.py tests/test_config.py tests/test_router.py tests/test_readme_api.py tests/test_readme_config.py -q && mise run lint`.

## Notes

- Reuse the matcher validator pattern (`HooksConfig._matcher_events`) for the onTick-only keys.
- Use the session clock for elapsed time; never let a tick task outlive the router.
