---
title: Deflake never typed while busy
status: in-progress
repo: .
value: A flaky test in the runner suite would make CI red at random
files: [tests/test_command_runner.py]
verify: "uv run pytest tests/test_command_runner.py -q && mise run lint"
depends_on: [command-runner-failures]
owns: []
uses: []
model: sonnet
estimate: 45
actual: ""
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/agent-callable-slash-command--deflake-never-typed-while-bu
branch: agent-callable-slash-command--deflake-never-typed-while-bu
verified_head: ""
run: ""
provides: []
---

# Deflake never typed while busy

## Requirement

tests/test_command_runner.py::test_never_typed_while_busy fails intermittently under the full suite (seen once in `mise run test` on main f874f1e: 1 of 1019) and passes 3/3 alone. Find the timing dependency and make the test deterministic without weakening what it asserts (a queued command is never typed while claude is responding). No production behaviour change; if the root cause is a real race in the runner (router.py), report it as discovered instead of editing router.py here.

## Acceptance conditions

- AC1: the test passes 30 consecutive runs [evidence: command:for i in $(seq 1 30); do uv run pytest -q -p no:cacheprovider tests/test_command_runner.py::test_never_typed_while_busy || exit 1; done]
- AC2: lint and the full suite stay green [evidence: command:mise run lint && mise run test]

## Files

- `tests/test_command_runner.py`

## Interfaces

Keeps the command runner contract (router.py command runner, session.command_active); test name unchanged.

## Expected outcome

test_never_typed_while_busy is deterministic: 30/30 runs pass and the full suite is green.

## Steps

- Reproduce under load (e.g. run it with `-p no:randomly` alongside tests/test_command_runner.py, or loop 30x) and find the wait or sleep it depends on.
- Replace timing assumptions with explicit synchronisation (await the event or state), or fix the runner race if real.
- Run the AC1 loop (30 passes), then `mise run lint && mise run test`.

## Notes

Seen failing once in the full suite on main f874f1e (1016 passed, 3 failed; the other two were a StubSession gap fixed in f874f1e). Do not edit tests/conftest.py or tests/fake_claude.
