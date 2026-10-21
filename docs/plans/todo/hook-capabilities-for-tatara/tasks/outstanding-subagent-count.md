---
title: Outstanding subagent count
status: todo
repo: .
value: "The session snapshot shows how many subagents are still running (K10)"
files: [src/claude_code_wrapper/router.py, src/claude_code_wrapper/manager.py, README.md, tests/test_outstanding_subagents.py]
verify: "uv run pytest tests/test_outstanding_subagents.py tests/test_api_sessions.py -q && mise run lint"
depends_on: [probe-token-and-answer]
owns: []
uses: [ccw-world-fixture]
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

# Outstanding subagent count

## Requirement

K10 outstanding subagents in the snapshot (design D14).

- The router keeps the set of `agent_id`s seen in `SubagentStart` and not yet in `SubagentStop` (a Stop for an unknown id is ignored). The set is cleared when claude restarts (`on_restart`) and when the router closes.
- The session snapshot (`GET /v1/sessions/{id}`, the list) exposes `outstanding_subagents: int`; 0 for stopped and persisted sessions.
- README: the field in the GET session response.

## Acceptance conditions

- AC1: the count rises on SubagentStart and falls on SubagentStop [evidence: test:tests/test_outstanding_subagents.py::test_count_rises_and_falls_with_subagent_hooks]
- AC2: an unknown or repeated SubagentStop never goes below zero [evidence: test:tests/test_outstanding_subagents.py::test_duplicate_stop_does_not_go_negative]
- AC3: a claude restart clears the count [evidence: test:tests/test_outstanding_subagents.py::test_restart_clears_count]
- AC4: a stopped session reports zero [evidence: test:tests/test_outstanding_subagents.py::test_stopped_session_reports_zero]
- AC5: README documents outstanding_subagents [evidence: command:grep -q outstanding_subagents README.md]

## Files

- `src/claude_code_wrapper/router.py`
- `src/claude_code_wrapper/manager.py`
- `README.md`
- `tests/test_outstanding_subagents.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
