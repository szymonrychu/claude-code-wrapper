---
title: Hook relay
status: todo
repo: claude-code-wrapper
value: Claude hooks reach the service without ever blocking claude
files: [src/claude_code_wrapper/hook_relay.py, tests/test_hook_relay.py]
verify: "uv run pytest tests/test_hook_relay.py -q && mise run lint"
depends_on: [repo-scaffold]
owns: [hook-relay-protocol]
uses: []
model: ""
estimate: 45
actual: ""
review: full
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Hook relay

## Requirement

Build the Claude hook relay `ccw-hook` and the relay protocol (D7) in `src/claude_code_wrapper/hook_relay.py`. Stdlib only (urllib, json, sys, os, time) so it starts fast inside every hook call.

Protocol constants (part `hook-relay-protocol`): `CLAUDE_HOOK_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop", "SubagentStart", "SubagentStop")`, `RELAY_PATH = "/internal/hooks/{event}"`, `HEADER_SESSION = "X-CCW-Session"`, `HEADER_TOKEN = "X-CCW-Hook-Token"`, env names `ENV_URL = "CCW_INTERNAL_URL"`, `ENV_SESSION = "CCW_SESSION_ID"`, `ENV_TOKEN = "CCW_HOOK_TOKEN"`, `ENV_GATE_FAIL = "CCW_TOOL_GATE_FAIL"`, `ENV_GATE_TIMEOUT = "CCW_GATE_TIMEOUT"`; `RelayResponse` TypedDict `{stdout: str, stderr: str, exit_code: int}`; helper `deny_output(reason) -> str` producing `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}`.

`main(argv=None) -> int` (console script, also `python -m claude_code_wrapper.hook_relay <Event>`):
- argv[1] is the event; an unknown event exits 0 with no output.
- Reads stdin bytes (capped at 8 MiB).
- Missing `CCW_INTERNAL_URL` or `CCW_SESSION_ID` -> exit 0, no output.
- POSTs the stdin bytes verbatim to `<url>/internal/hooks/<Event>` with the two headers and `Content-Type: application/json`.
- 200 with a RelayResponse JSON: writes `stdout` to stdout, `stderr` to stderr, exits `exit_code`.
- Retries on connection errors, timeouts and non-200: Stop and SubagentStop 5 attempts with 5 s timeout; PreToolUse 2 attempts with timeout `CCW_GATE_TIMEOUT` seconds (default 60); others 3 attempts with 5 s; 0.5 s backoff doubling.
- After the last failure: exit 0 with no output, except PreToolUse when `CCW_TOOL_GATE_FAIL=deny`, which prints `deny_output("claude-code-wrapper is unreachable")` and exits 0.
- Never raises; any unexpected exception ends in the failure path above.

## Acceptance conditions

- AC1: The relay forwards the payload verbatim with session and token headers [evidence: test:tests/test_hook_relay.py::test_forwards_payload_and_headers]
- AC2: The relay prints the returned stdout and exits with the returned code [evidence: test:tests/test_hook_relay.py::test_prints_stdout_and_exit_code]
- AC3: Transient failures are retried until success [evidence: test:tests/test_hook_relay.py::test_retries_then_succeeds]
- AC4: An unreachable service gives exit 0 and no output [evidence: test:tests/test_hook_relay.py::test_unreachable_exits_0_silently]
- AC5: PreToolUse with CCW_TOOL_GATE_FAIL=deny prints a deny when unreachable [evidence: test:tests/test_hook_relay.py::test_pretooluse_fail_closed_prints_deny]
- AC6: Missing env vars give exit 0 and no output [evidence: test:tests/test_hook_relay.py::test_missing_env_exits_0]
- AC7: The relay imports no third-party module [evidence: test:tests/test_hook_relay.py::test_stdlib_only]

## Files

- `src/claude_code_wrapper/hook_relay.py`
- `tests/test_hook_relay.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
