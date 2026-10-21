---
title: "mise shim for ruff has no version set (mise ERROR No version is set for shim: ruff), so `mise run lint` fails in the verifier environment"
status: todo
repo: .
value: "Independent verification used mise-resolved ruff and failed; `uv run ruff` works. Needs a mise/ruff pin or a lint task using uv run."
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

# mise shim for ruff has no version set (mise ERROR No version is set for shim: ruff), so `mise run lint` fails in the verifier environment

## Requirement

mise shim for ruff has no version set (mise ERROR No version is set for shim: ruff), so `mise run lint` fails in the verifier environment

Where: mise.toml / verifier environment

Why: Independent verification used mise-resolved ruff and failed; `uv run ruff` works. Needs a mise/ruff pin or a lint task using uv run.

## Acceptance conditions

- AC1: mise shim for ruff has no version set (mise ERROR No version is set for shim: ruff), so `mise run lint` fails in the verifier environment at mise.toml / verifier environment is resolved [evidence: judgement]

## Files



## Interfaces

Keep the interfaces around mise.toml / verifier environment unchanged unless resolving it requires otherwise: mise shim for ruff has no version set (mise ERROR No version is set for shim: ruff), so `mise run lint` fails in the verifier environment.

## Expected outcome

mise shim for ruff has no version set (mise ERROR No version is set for shim: ruff), so `mise run lint` fails in the verifier environment at mise.toml / verifier environment is resolved, and Independent verification used mise-resolved ruff and failed; `uv run ruff` works. Needs a mise/ruff pin or a lint task using uv run. no longer applies.

## Steps

1. Write a failing test that shows: mise shim for ruff has no version set (mise ERROR No version is set for shim: ruff), so `mise run lint` fails in the verifier environment.
2. Fix it at mise.toml / verifier environment.
3. Run the task verify command and confirm it passes.

## Notes

Discovered in task `tests-test-session-boot-py-t` during run `hook-capabilities-for-tatara`.

