---
title: Destroy hook input output
status: todo
repo: .
value: "DELETE can feed an onDestroy hook and return its output, e.g. the magic-trainer review (K3)"
files: [src/claude_code_wrapper/api/sessions.py, src/claude_code_wrapper/manager.py, src/claude_code_wrapper/router.py, src/claude_code_wrapper/events.py, README.md, tests/test_destroy_io.py]
verify: "uv run pytest tests/test_destroy_io.py tests/test_api_sessions.py tests/test_manager.py tests/test_router.py tests/test_readme_api.py -q && mise run lint"
depends_on: [turn-continuity-across-crash]
owns: [destroy-io]
uses: [hook-result-fields, ccw-world-fixture]
model: sonnet
estimate: 75
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Destroy hook input output

## Requirement

K3 destroy hook input and output (design D7). Owns part `destroy-io`.

- `DELETE /v1/sessions/{id}` accepts an optional JSON body `{"hook_input": <object>}` (`extra="forbid"`). A body above 1 MiB: 413 `too_large`. No body: as today.
- `SessionManager.destroy(session_id, purge=False, hook_input=None)`: onDestroy data = the stopped snapshot plus `input` (the hook_input, null when absent).
- The DELETE response gains `hook_outputs: [{index, ok, output, error}]` from the blocking onDestroy actions (the `HookOutcome.outputs` of hook-result-fields); `[]` when there are none or the session was not active.
- A `destroy_output` event `{outputs}` is appended after onDestroy and before `session_closed`. To get that order, `session_closed` is emitted by the manager after onDestroy instead of by `SessionRouter.close()` during teardown (the router's close gains a flag or the emit moves); every destroy path, including the shutdown path and abandon, still emits exactly one `session_closed`.
- On shutdown (SIGTERM) onDestroy runs with `input: null` under its 25% share of `shutdown_timeout_seconds`; `destroy_output` is still emitted when it completes in time.
- Timing stays: onDestroy runs after claude stopped and before purge, and DELETE returns after it.
- `destroy_output` joins `EVENT_TYPES`.
- README: DELETE request body and response, timing and bounds (DELETE: action `timeout_seconds`; SIGTERM: 25% of `shutdown_timeout_seconds`, so long reviews need DELETE), onDestroy `data.input`, and `### \`destroy_output\``.

## Acceptance conditions

- AC1: the DELETE body hook_input reaches onDestroy data.input [evidence: test:tests/test_destroy_io.py::test_delete_body_reaches_on_destroy_input]
- AC2: the DELETE response lists hook_outputs of blocking onDestroy actions [evidence: test:tests/test_destroy_io.py::test_delete_returns_hook_outputs]
- AC3: destroy_output is appended before session_closed and session_closed appears once [evidence: test:tests/test_destroy_io.py::test_destroy_output_event_before_session_closed]
- AC4: DELETE without a body behaves as before with empty hook_outputs [evidence: test:tests/test_destroy_io.py::test_delete_without_body_unchanged]
- AC5: a body above 1 MiB is rejected with 413 [evidence: test:tests/test_destroy_io.py::test_delete_body_too_large_413]
- AC6: a failed onDestroy action reports its error in hook_outputs [evidence: test:tests/test_destroy_io.py::test_failed_action_output_has_error]
- AC7: shutdown still emits destroy_output then session_closed [evidence: test:tests/test_destroy_io.py::test_shutdown_emits_destroy_output]
- AC8: existing sessions, manager and router tests stay green [evidence: command:uv run pytest tests/test_api_sessions.py tests/test_manager.py tests/test_router.py tests/test_readme_api.py -q]

## Files

- `src/claude_code_wrapper/api/sessions.py`
- `src/claude_code_wrapper/manager.py`
- `src/claude_code_wrapper/router.py`
- `src/claude_code_wrapper/events.py`
- `README.md`
- `tests/test_destroy_io.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
