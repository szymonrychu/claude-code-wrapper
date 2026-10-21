---
title: onTick timed hook
status: todo
repo: .
value: "Periodic hook actions for safety pushes and metrics pushes without sidecars (K7)"
files: [src/claude_code_wrapper/config.py, src/claude_code_wrapper/hooks.py, src/claude_code_wrapper/router.py, README.md, tests/test_on_tick.py]
verify: "uv run pytest tests/test_on_tick.py tests/test_config.py tests/test_router.py tests/test_readme_api.py tests/test_readme_config.py -q && mise run lint"
depends_on: [turn-metadata-on-submit]
owns: [on-tick-event]
uses: [ccw-world-fixture]
model: sonnet
estimate: 75
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# onTick timed hook

## Requirement

K7 onTick timed hook (design D11). Owns part `on-tick-event`.

- `onTick` joins `HOOK_EVENTS` and `HooksConfig`. Hook actions gain `interval_seconds: float | None` (> 0) and `when: Literal["busy", "always"] | None`. On onTick `interval_seconds` is required and `when` defaults to `busy`; on every other event both are rejected. Errors point at `hooks.onTick.<i>.<type>.interval_seconds` like the matcher validator.
- The router owns one timer task per onTick action, started when the session first reaches ready, kept across claude restarts and cancelled in `close()`. Each tick fires only that action (`HookDispatcher.fire` gains a way to run a single action index). `when: busy` skips ticks with no running turn. A tick whose previous run of the same action has not finished is skipped (no overlap).
- data: `turn_id` (null when idle), `elapsed_seconds` of the running turn (null when idle), `interval_seconds`.
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

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
