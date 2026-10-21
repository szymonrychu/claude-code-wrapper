---
title: Deflake never typed while busy
status: todo
repo: .
value: A flaky test in the runner suite would make CI red at random
files: [tests/test_command_runner.py, src/claude_code_wrapper/router.py]
verify: "uv run pytest tests/test_command_runner.py -q && mise run lint"
depends_on: [command-runner-failures]
owns: []
uses: []
model: sonnet
estimate: 45
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Deflake never typed while busy

## Requirement

tests/test_command_runner.py::test_never_typed_while_busy fails intermittently under the full suite (seen once in `mise run test` on main f874f1e: 1 of 1019) and passes 3/3 alone. Find the timing dependency and make the test deterministic without weakening what it asserts (a queued command is never typed while claude is responding). No production behaviour change unless the root cause is a real race in the runner, in which case fix the race and say so.

## Acceptance conditions

- AC1: the test passes 30 consecutive runs [evidence: command:for i in $(seq 1 30); do uv run pytest -q -p no:cacheprovider tests/test_command_runner.py::test_never_typed_while_busy || exit 1; done]
- AC2: lint and the full suite stay green [evidence: command:mise run lint && mise run test]

## Files

- `tests/test_command_runner.py`
- `src/claude_code_wrapper/router.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
