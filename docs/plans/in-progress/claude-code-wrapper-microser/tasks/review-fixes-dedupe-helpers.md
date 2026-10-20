---
title: Review fixes dedupe helpers
status: done
repo: claude-code-wrapper
value: One timestamp format and no duplicated helpers or example configs
files: [src/claude_code_wrapper/turns.py, src/claude_code_wrapper/events.py, src/claude_code_wrapper/hooks.py, src/claude_code_wrapper/session.py, src/claude_code_wrapper/api/turns.py, src/claude_code_wrapper/data/config.example.yaml, config.example.yaml, tests/test_timestamps.py]
verify: "uv run pytest -q && mise run lint"
depends_on: [review-fixes-auth-compose]
owns: []
uses: []
model: ""
estimate: 60
actual: ""
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/claude-code-wrapper-microser--review-fixes-dedupe-helpers
branch: claude-code-wrapper-microser--review-fixes-dedupe-helpers
verified_head: a88682a7812754e6e8c0f2250967b258eae94fe9
run: claude-code-wrapper-microser
provides: []
acs_for_audit: []
process_gaps: ["AC evidence tests for items 1-5 live in tests/test_timestamps.py, not tests/test_packaging.py, so the verifier's AC1-AC4 mapping to test_packaging.py does not resolve."]
---

# Review fixes dedupe helpers

## Requirement

Epic-review remediation (whole-diff review, quality). Remove duplicated contracts and helpers found across modules on main cdc33c2:

1. Timestamps: one helper produces every service timestamp. turns.py:47 `now_iso()` returns `+00:00`, while events.py:64 and hooks.py:48 build their own and use `Z`. Make a single `now_iso()` (in one module, e.g. a small `timeutil.py` or turns.py) that returns RFC3339 UTC with `Z`, use it in turns, events and hooks, and make metrics parsing accept it. Turn records and events of one session then carry the same format.
2. `_squash` / `_WS` exist identically in ring.py:26/37 and session.py:77/267; keep one (ring.py) and import it in session.py.
3. `_out` (redactor wrapper) exists in api/sessions.py:29 and api/turns.py:50; keep one shared helper in the api package.
4. The example config hook `run: "./guard.sh"` (onAgentToolCall) points at a file that ships nowhere: replace it with a self-contained example (e.g. an inline `sh -c`-style command reading the payload from stdin) or ship the script under examples/ and reference it correctly.
5. There are two copies of the example config (repo root config.example.yaml and src/claude_code_wrapper/data/config.example.yaml). Keep exactly one source of truth (the packaged data copy) and update every reference (README, Dockerfile, compose, tests); or, if a root copy must stay, a test asserts the two are byte-identical. Prefer one copy.
No behaviour change beyond the unified timestamp format; existing tests stay green.


6. Regression from review-fixes-auth-compose (already on this branch at 9e8e8ea): `tests/test_packaging.py::test_serve_preflight_missing_binaries` now fails because its config has no `server.auth_token`, so startup stops at the auth policy before the binary preflight. Fix the test fixture config (set an auth_token, or allow_anonymous: true) so it again proves the preflight names the missing binary; do not reorder production checks unless the preflight should logically run first (decide and state why). Also add the git-missing branch of the preflight (only the claude-missing path is tested today). The full suite `uv run pytest` must be green.

## Acceptance conditions

- AC1: Turn records and events for one session use the same Z-suffixed RFC3339 format from one helper [evidence: test:tests/test_timestamps.py::test_turns_and_events_share_timestamp_format]
- AC2: There is exactly one definition each of now_iso, _squash and the api _out helper in src [evidence: test:tests/test_timestamps.py::test_no_duplicate_helpers]
- AC3: The example config's onAgentToolCall hook is runnable as shipped (no missing script) [evidence: test:tests/test_config.py::test_example_hooks_reference_existing_commands]
- AC4: Only one example config source exists, or the copies are asserted identical [evidence: test:tests/test_packaging.py::test_single_example_config_source]
- AC5: The serve preflight tests pass with a valid auth config and cover both the missing claude binary and missing git [evidence: test:tests/test_packaging.py::test_serve_preflight_missing_binaries]
- AC6: The full suite is green on this branch [evidence: command:uv run pytest -q]

## Files

- `src/claude_code_wrapper/turns.py`
- `src/claude_code_wrapper/events.py`
- `src/claude_code_wrapper/hooks.py`
- `src/claude_code_wrapper/session.py`
- `src/claude_code_wrapper/api/turns.py`
- `src/claude_code_wrapper/data/config.example.yaml`
- `config.example.yaml`
- `tests/test_timestamps.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

Every acceptance condition above passes on main after merge; full `mise run test` and `mise run lint` stay green.

What is true and observable when the task is done.

## Steps

1. Red: add/rename the evidence tests and see the new ones fail for the stated reason.
2. Green: smallest change that passes them.
3. Refactor with the suite green; run `mise run test` and `mise run lint`.

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.

- 2026-10-20 amended: run6: full-suite regression in test_packaging preflight from auth change; verify agent died
