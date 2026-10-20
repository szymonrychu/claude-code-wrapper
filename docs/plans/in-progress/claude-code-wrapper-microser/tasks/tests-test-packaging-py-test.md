---
title: "tests/test_packaging.py::test_serve_preflight_missing_binaries fails: ccw serve now exits 'server.auth_token: empty' before reaching the missing-binary preflight the test expects"
status: todo
repo: claude-code-wrapper
value: "Introduced by review-fixes-auth-compose; the test must set an auth token or allow_anonymous"
files: [tests/test_packaging.py]
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

# tests/test_packaging.py::test_serve_preflight_missing_binaries fails: ccw serve now exits 'server.auth_token: empty' before reaching the missing-binary preflight the test expects

## Requirement

tests/test_packaging.py::test_serve_preflight_missing_binaries fails: ccw serve now exits 'server.auth_token: empty' before reaching the missing-binary preflight the test expects

Where: tests/test_packaging.py

Why: Introduced by review-fixes-auth-compose; the test must set an auth token or allow_anonymous

## Acceptance conditions

- AC1: tests/test_packaging.py::test_serve_preflight_missing_binaries fails: ccw serve now exits 'server.auth_token: empty' before reaching the missing-binary preflight the test expects at tests/test_packaging.py is resolved [evidence: judgement]

## Files

- `tests/test_packaging.py`

## Interfaces

Keep the interfaces around tests/test_packaging.py unchanged unless resolving it requires otherwise: tests/test_packaging.py::test_serve_preflight_missing_binaries fails: ccw serve now exits 'server.auth_token: empty' before reaching the missing-binary preflight the test expects.

## Expected outcome

tests/test_packaging.py::test_serve_preflight_missing_binaries fails: ccw serve now exits 'server.auth_token: empty' before reaching the missing-binary preflight the test expects at tests/test_packaging.py is resolved, and Introduced by review-fixes-auth-compose; the test must set an auth token or allow_anonymous no longer applies.

## Steps

1. Write a failing test that shows: tests/test_packaging.py::test_serve_preflight_missing_binaries fails: ccw serve now exits 'server.auth_token: empty' before reaching the missing-binary preflight the test expects.
2. Fix it at tests/test_packaging.py.
3. Run the task verify command and confirm it passes.

## Notes

Discovered in task `review-fixes-dedupe-helpers` during run `claude-code-wrapper-microser`.

