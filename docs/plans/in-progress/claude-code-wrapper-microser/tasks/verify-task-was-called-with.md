---
title: "verify-task was called with --prev-head unknown, crashed in git diff before running any test, and its crash message was then reported as two code findings (failed verification, unclean worktree)."
status: todo
repo: claude-code-wrapper
value: "With no real prev-head the verifier cannot run, and a harness error turns into false high findings that cost a whole fix round. The workflow should pass the task's pre-implementation SHA (here 900a8e8) or fall back to the merge-base with main."
files: []
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

# verify-task was called with --prev-head unknown, crashed in git diff before running any test, and its crash message was then reported as two code findings (failed verification, unclean worktree).

## Requirement

verify-task was called with --prev-head unknown, crashed in git diff before running any test, and its crash message was then reported as two code findings (failed verification, unclean worktree).

Where: workbench plan-execute workflow, verify-task invocation

Why: With no real prev-head the verifier cannot run, and a harness error turns into false high findings that cost a whole fix round. The workflow should pass the task's pre-implementation SHA (here 900a8e8) or fall back to the merge-base with main.

## Acceptance conditions

- AC1: verify-task was called with --prev-head unknown, crashed in git diff before running any test, and its crash message was then reported as two code findings (failed verification, unclean worktree). at workbench plan-execute workflow, verify-task invocation is resolved [evidence: judgement]

## Files



## Interfaces

Keep the interfaces around workbench plan-execute workflow, verify-task invocation unchanged unless resolving it requires otherwise: verify-task was called with --prev-head unknown, crashed in git diff before running any test, and its crash message was then reported as two code findings (failed verification, unclean worktree)..

## Expected outcome

verify-task was called with --prev-head unknown, crashed in git diff before running any test, and its crash message was then reported as two code findings (failed verification, unclean worktree). at workbench plan-execute workflow, verify-task invocation is resolved, and With no real prev-head the verifier cannot run, and a harness error turns into false high findings that cost a whole fix round. The workflow should pass the task's pre-implementation SHA (here 900a8e8) or fall back to the merge-base with main. no longer applies.

## Steps

1. Write a failing test that shows: verify-task was called with --prev-head unknown, crashed in git diff before running any test, and its crash message was then reported as two code findings (failed verification, unclean worktree)..
2. Fix it at workbench plan-execute workflow, verify-task invocation.
3. Run the task verify command and confirm it passes.

## Notes

Discovered in task `e2e-lifecycle` during run `claude-code-wrapper-microser`.

