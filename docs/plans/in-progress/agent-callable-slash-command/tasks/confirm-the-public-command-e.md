---
title: "Confirm the public command endpoint maps CommandRejected invalid_state to HTTP 409; api/errors.py has no CommandRejected mapping in this tree"
status: todo
repo: .
value: "The requirement says the endpoint maps invalid_state to 409, but the endpoint belongs to another task"
files: [src/claude_code_wrapper/api/errors.py]
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

# Confirm the public command endpoint maps CommandRejected invalid_state to HTTP 409; api/errors.py has no CommandRejected mapping in this tree

## Requirement

Confirm the public command endpoint maps CommandRejected invalid_state to HTTP 409; api/errors.py has no CommandRejected mapping in this tree

Where: src/claude_code_wrapper/api/errors.py

Why: The requirement says the endpoint maps invalid_state to 409, but the endpoint belongs to another task

## Acceptance conditions

- AC1: Confirm the public command endpoint maps CommandRejected invalid_state to HTTP 409; api/errors.py has no CommandRejected mapping in this tree at src/claude_code_wrapper/api/errors.py is resolved [evidence: judgement]

## Files

- `src/claude_code_wrapper/api/errors.py`

## Interfaces

Keep the interfaces around src/claude_code_wrapper/api/errors.py unchanged unless resolving it requires otherwise: Confirm the public command endpoint maps CommandRejected invalid_state to HTTP 409; api/errors.py has no CommandRejected mapping in this tree.

## Expected outcome

Confirm the public command endpoint maps CommandRejected invalid_state to HTTP 409; api/errors.py has no CommandRejected mapping in this tree at src/claude_code_wrapper/api/errors.py is resolved, and The requirement says the endpoint maps invalid_state to 409, but the endpoint belongs to another task no longer applies.

## Steps

1. Write a failing test that shows: Confirm the public command endpoint maps CommandRejected invalid_state to HTTP 409; api/errors.py has no CommandRejected mapping in this tree.
2. Fix it at src/claude_code_wrapper/api/errors.py.
3. Run the task verify command and confirm it passes.

## Notes

Discovered in task `command-runner-failures` during run `agent-callable-slash-command`.

