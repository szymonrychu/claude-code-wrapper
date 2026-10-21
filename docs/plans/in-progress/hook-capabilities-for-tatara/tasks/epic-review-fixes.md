---
title: Epic review fixes
status: todo
repo: .
value: Closes the fail-open and hardening findings of the epic review before the epic closes
files: [src/claude_code_wrapper/router.py, src/claude_code_wrapper/hooks.py, src/claude_code_wrapper/manager.py, src/claude_code_wrapper/data/examples/tatara/tatara_hooks.py, tests/test_epic_review_fixes.py, README.md]
verify: "uv run pytest tests/test_epic_review_fixes.py -q && mise run lint && mise run test"
depends_on: []
owns: []
uses: []
model: sonnet
estimate: 60
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Epic review fixes

## Requirement

Epic-review remediation (whole-diff review + security review, 2026-10-21). Fix all five, test-first:
1. router.py PreToolUse fail-closed with a missing or empty `tool_name`: gate_fail_mode must consider every blocking onAgentToolCall action (pass None) when the tool name is empty, so a ccw-side exception denies when any on_error deny gate exists.
2. hooks.py `_run`: a failed blocking onStop action with on_error deny must yield decision `block` with the failure reason (not be downgraded to allow as an invalid decision); document it in the README onStop section.
3. router.py handle_claude_hook PreToolUse except-branch: compute and return the fail-closed deny first; fire onError without awaiting it on the relay path (spawn it), so slow onError hooks cannot outlast the relay timeout.
4. manager.py hook state dir: create CCW_STATE_DIR with mode 0o700.
5. data/examples/tatara/tatara_hooks.py: refuse to POST the signed callback when CALLBACK_HMAC_SECRET is empty (log to stderr, exit non-zero) instead of signing with an empty key. Keep the body-only HMAC format (it matches the tatara operator contract; no timestamp).

## Acceptance conditions

- AC1: a PreToolUse payload without tool_name plus a dispatcher exception is denied when a matcher-scoped on_error deny gate exists [evidence: test:tests/test_epic_review_fixes.py::test_pretooluse_missing_tool_name_fails_closed]
- AC2: a crashing blocking onStop action with on_error deny blocks the Stop with its reason [evidence: test:tests/test_epic_review_fixes.py::test_onstop_on_error_deny_blocks]
- AC3: the PreToolUse fail-closed deny is returned without waiting for a slow onError hook [evidence: test:tests/test_epic_review_fixes.py::test_pretooluse_deny_not_delayed_by_onerror]
- AC4: CCW_STATE_DIR is created with mode 0o700 [evidence: test:tests/test_epic_review_fixes.py::test_state_dir_mode_0700]
- AC5: the tatara finaliser refuses to post with an empty CALLBACK_HMAC_SECRET [evidence: test:tests/test_epic_review_fixes.py::test_tatara_finaliser_refuses_empty_secret]
- AC6: README onStop section states that on_error deny maps to block [evidence: command:grep -qi "on_error: deny" README.md]
- AC7: lint and the full suite stay green [evidence: command:mise run lint && mise run test]

## Files

- `src/claude_code_wrapper/router.py`
- `src/claude_code_wrapper/hooks.py`
- `src/claude_code_wrapper/manager.py`
- `src/claude_code_wrapper/data/examples/tatara/tatara_hooks.py`
- `tests/test_epic_review_fixes.py`
- `README.md`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
