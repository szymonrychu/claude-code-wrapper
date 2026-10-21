---
title: Busy check before turn files
status: in-progress
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
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/hook-capabilities-for-tatara--busy-check-before-turn-files
branch: hook-capabilities-for-tatara--busy-check-before-turn-files
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

- `session.py::Session.check_submittable() -> None` raising `TurnRejected`.
- `router.py::SessionRouter.submit_turn` order: check, onTurn, check, write files, submit.

## Expected outcome

A 409 busy (or 503) turn never touches the workdir and fires no onTurn hook.

## Steps

- Write `tests/test_submit_busy_first.py` (a busy session with a file present; submit with that file; assert content unchanged and no onTurn hook_result); see it fail.
- Add `check_submittable`, call it twice in `submit_turn`.
- `uv run pytest tests/test_submit_busy_first.py tests/test_router.py tests/test_session_turns.py tests/test_api_turns.py -q && mise run lint`.

## Notes

- `Session.submit` reserves the slot before its first await; keep the second check, `safe_write_files` and the `submit` call free of awaits between them.
