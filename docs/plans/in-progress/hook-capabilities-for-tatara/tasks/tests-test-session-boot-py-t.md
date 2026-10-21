---
title: "tests/test_session_boot.py::test_restart_budget_exhausted_dead looks flaky: it once asserted rec.exits == [3] and got [], and passed on every rerun here"
status: todo
repo: .
value: "The check is timing-dependent (wait_until on state then assert exits), so it can intermittently fail the verify command."
files: ["tests/test_session_boot.py:555"]
verify: ""
depends_on: []
owns: []
uses: []
model: ""
estimate: ""
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# tests/test_session_boot.py::test_restart_budget_exhausted_dead looks flaky: it once asserted rec.exits == [3] and got [], and passed on every rerun here

## Requirement

tests/test_session_boot.py::test_restart_budget_exhausted_dead looks flaky: it once asserted rec.exits == [3] and got [], and passed on every rerun here

Where: tests/test_session_boot.py:555

Why: The check is timing-dependent (wait_until on state then assert exits), so it can intermittently fail the verify command.

## Acceptance conditions

- AC1: tests/test_session_boot.py::test_restart_budget_exhausted_dead looks flaky: it once asserted rec.exits == [3] and got [], and passed on every rerun here at tests/test_session_boot.py:555 is resolved [evidence: judgement]

## Files

- `tests/test_session_boot.py:555`

## Interfaces

Keep the interfaces around tests/test_session_boot.py:555 unchanged unless resolving it requires otherwise: tests/test_session_boot.py::test_restart_budget_exhausted_dead looks flaky: it once asserted rec.exits == [3] and got [], and passed on every rerun here.

## Expected outcome

tests/test_session_boot.py::test_restart_budget_exhausted_dead looks flaky: it once asserted rec.exits == [3] and got [], and passed on every rerun here at tests/test_session_boot.py:555 is resolved, and The check is timing-dependent (wait_until on state then assert exits), so it can intermittently fail the verify command. no longer applies.

## Steps

1. Write a failing test that shows: tests/test_session_boot.py::test_restart_budget_exhausted_dead looks flaky: it once asserted rec.exits == [3] and got [], and passed on every rerun here.
2. Fix it at tests/test_session_boot.py:555.
3. Run the task verify command and confirm it passes.

## Notes

Discovered in task `passthrough-env-globs` during run `hook-capabilities-for-tatara`.

