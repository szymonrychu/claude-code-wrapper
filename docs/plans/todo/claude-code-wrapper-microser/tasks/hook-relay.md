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
- Retries on connection errors, timeouts and non-200: UserPromptSubmit 3 attempts with 5 s timeout; every other event 2 attempts, each with timeout `CCW_GATE_TIMEOUT` seconds (default 14; the session sets it from `relay_timeouts`), 0.5 s backoff doubling. Every attempt of one invocation sends the same `X-CCW-Delivery` id (uuid4 generated once per invocation; constant `HEADER_DELIVERY = "X-CCW-Delivery"`) so the service can dedupe retries.
- After the last failure: exit 0 with no output, except PreToolUse when `CCW_TOOL_GATE_FAIL=deny`, which prints `deny_output("claude-code-wrapper is unreachable")` and exits 0.
- Never raises; any unexpected exception ends in the failure path above.

Settled by the builder check (binding):
- `HEADER_DELIVERY = "X-CCW-Delivery"` is part of the protocol; one uuid4 per invocation, identical on every retry.
- Long per-attempt timeout (`CCW_GATE_TIMEOUT`) applies to every event whose user hooks may block (all but UserPromptSubmit).

## Acceptance conditions

- AC1: The relay forwards the payload verbatim with session and token headers [evidence: test:tests/test_hook_relay.py::test_forwards_payload_and_headers]
- AC2: The relay prints the returned stdout and exits with the returned code [evidence: test:tests/test_hook_relay.py::test_prints_stdout_and_exit_code]
- AC3: Transient failures are retried until success [evidence: test:tests/test_hook_relay.py::test_retries_then_succeeds]
- AC4: An unreachable service gives exit 0 and no output [evidence: test:tests/test_hook_relay.py::test_unreachable_exits_0_silently]
- AC5: PreToolUse with CCW_TOOL_GATE_FAIL=deny prints a deny when unreachable [evidence: test:tests/test_hook_relay.py::test_pretooluse_fail_closed_prints_deny]
- AC6: Missing env vars give exit 0 and no output [evidence: test:tests/test_hook_relay.py::test_missing_env_exits_0]
- AC7: The relay imports no third-party module [evidence: test:tests/test_hook_relay.py::test_stdlib_only]

- AC8: The delivery id is identical across retries of one invocation [evidence: test:tests/test_hook_relay.py::test_delivery_id_stable_across_retries]

## Files

- `src/claude_code_wrapper/hook_relay.py`
- `tests/test_hook_relay.py`

## Interfaces

- Part `hook-relay-protocol` in `claude_code_wrapper.hook_relay`: constants above, `RelayResponse`, `deny_output`, `main`. Consumers: settings-render (event list, command), event-router (response shape), internal-api (path, headers).

## Expected outcome

Claude's hooks reach the service in milliseconds; when the service is down claude is never blocked (Stop exits 0) and the tool gate fails open or closed as configured.

## Steps

- Write `tests/test_hook_relay.py`: start an `http.server` stub on 127.0.0.1:0 in a thread (scripted responses), run `[sys.executable, "-m", "claude_code_wrapper.hook_relay", "<Event>"]` with env and stdin; red.
- Implement; green; `mise run lint`.

## Notes

- Sources: tatara `W/cmd/cc-stop-hook/main.go` (5 x 5 s, always exit 0), magic `M/stop_hook.py:33-104`.
- Keep test retry delays short via an env override such as `CCW_RELAY_BACKOFF` (document it in the module docstring) so the suite stays fast.
