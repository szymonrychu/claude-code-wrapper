---
title: Epic review fixes
status: escalated
repo: .
value: Closes the fail-open and hardening findings of the epic review before the epic closes
files: [src/claude_code_wrapper/router.py, src/claude_code_wrapper/hooks.py, src/claude_code_wrapper/manager.py, src/claude_code_wrapper/data/examples/tatara/tatara_hooks.py, tests/test_epic_review_fixes.py, README.md]
verify: "uv run pytest tests/test_epic_review_fixes.py -q && mise run lint && mise run test"
depends_on: [magic-trainer-example, tests-test-session-boot-py-t]
owns: []
uses: []
model: sonnet
estimate: 60
actual: ""
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/hook-capabilities-for-tatara--epic-review-fixes
branch: hook-capabilities-for-tatara--epic-review-fixes
verified_head: unknown
run: hook-capabilities-for-tatara
provides: []
acs_for_audit: []
process_gaps: []
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

- AC1: a PreToolUse payload without tool_name plus a dispatcher exception is denied when a matcher-scoped on_error deny gate exists [evidence: test:tests/test_epic_review_fixes.py::test_pretooluse_empty_tool_name_fails_closed]
- AC2: a crashing blocking onStop action with on_error deny blocks the Stop with its reason [evidence: test:tests/test_epic_review_fixes.py::test_failed_blocking_onstop_with_deny_blocks]
- AC3: the PreToolUse fail-closed deny is returned without waiting for a slow onError hook [evidence: test:tests/test_epic_review_fixes.py::test_relay_deny_returned_before_slow_on_error]
- AC4: CCW_STATE_DIR is created with mode 0o700 [evidence: test:tests/test_epic_review_fixes.py::test_hook_state_dir_is_private]
- AC5: the tatara finaliser refuses to post with an empty CALLBACK_HMAC_SECRET [evidence: test:tests/test_epic_review_fixes.py::test_callback_refuses_empty_hmac_secret]
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

Keeps: router.gate_fail_mode(hooks, tool_name|None) semantics (None = every blocking onAgentToolCall action), hooks.EVENT_DECISIONS, the onStop decision set allow|block|defer, the relay deny output shape, tatara_hooks.py CLI subcommands.

## Expected outcome

No PreToolUse or onStop path fails open when a deny gate is configured; the fail-closed deny is not delayed by onError hooks; the hook state dir is 0700; the tatara finaliser never signs with an empty key. Full suite green.

## Steps

- Write tests/test_epic_review_fixes.py with the five AC-named tests; run `uv run pytest tests/test_epic_review_fixes.py -q`: expect 5 failures for the reasons in the requirement.
- Fix router.py (items 1, 3), hooks.py (item 2), manager.py (item 4), tatara_hooks.py (item 5); README onStop note (item 2).
- Run `uv run pytest tests/test_epic_review_fixes.py -q`: 5 passed; then `mise run lint && mise run test`: all passed.

## Notes

Findings come from the epic whole-diff review and security review (router.py ~366-372 except branch, hooks.py `_run` ~330-345, manager.py ~1179 state_dir mkdir, tatara_hooks.py ~282 CALLBACK_HMAC_SECRET). Use the existing `world` fixture (tests/conftest.py) and fake claude; do not edit tests/conftest.py or tests/fake_claude. Dismissed by the orchestrator: DELETE/turn body read before size cap (authenticated callers only), /metrics unauthenticated (by design), HMAC timestamp (would break the tatara operator contract).
