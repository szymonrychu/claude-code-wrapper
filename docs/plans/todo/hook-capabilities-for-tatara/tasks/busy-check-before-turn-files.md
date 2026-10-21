---
title: Busy check before turn files
status: todo
repo: .
value: "A rejected turn never rewrites files under the running turn (DEF-2)"
files: [src/claude_code_wrapper/session.py, src/claude_code_wrapper/router.py, tests/test_submit_busy_first.py]
verify: "uv run pytest tests/test_submit_busy_first.py tests/test_router.py tests/test_session_turns.py tests/test_api_turns.py -q && mise run lint"
depends_on: [pretooluse-gate-fails-closed]
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

# Busy check before turn files

## Requirement

DEF-2 (design D3): check busy before writing turn files.

Today `safe_write_files` runs in `SessionRouter.submit_turn` before `Session.submit` raises busy, so a rejected turn rewrites workdir files under the running turn.

- Add `Session.check_submittable()` (sync): raises `TurnRejected("booting" | "dead" | "busy")` with exactly the reasons and order `Session.submit` uses (writable PTY first, then busy).
- `SessionRouter.submit_turn` calls it before firing `onTurn` (a doomed turn fires no onTurn and no veto hooks), and again immediately before `safe_write_files`. There is no `await` between that second check, `safe_write_files` and `Session.submit`'s slot reservation.
- A rejected turn leaves the workdir untouched: an existing file keeps its content, a new file is not created.
- API status codes are unchanged (409 busy, 503 booting/dead).

## Acceptance conditions

- AC1: a busy submit writes no files and leaves existing content unchanged [evidence: test:tests/test_submit_busy_first.py::test_busy_submit_writes_no_files]
- AC2: a busy submit fires no onTurn hook [evidence: test:tests/test_submit_busy_first.py::test_busy_submit_fires_no_on_turn]
- AC3: booting and dead sessions are rejected before any file is written [evidence: test:tests/test_submit_busy_first.py::test_booting_and_dead_rejected_before_files]
- AC4: check_submittable raises the same reasons as submit [evidence: test:tests/test_submit_busy_first.py::test_check_submittable_matches_submit_reasons]
- AC5: existing router, session and turns API tests stay green [evidence: command:uv run pytest tests/test_router.py tests/test_session_turns.py tests/test_api_turns.py -q]

## Files

- `src/claude_code_wrapper/session.py`
- `src/claude_code_wrapper/router.py`
- `tests/test_submit_busy_first.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
