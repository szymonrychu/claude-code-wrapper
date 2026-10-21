---
title: Service exit policy
status: todo
repo: .
value: "The process exits when its last session is deleted or a session dies, for k8s Jobs and pod death signals (K8)"
files: [src/claude_code_wrapper/config.py, src/claude_code_wrapper/manager.py, src/claude_code_wrapper/service.py, src/claude_code_wrapper/cli.py, README.md, tests/test_exit_policy.py]
verify: "uv run pytest tests/test_exit_policy.py tests/test_service.py tests/test_cli.py tests/test_readme_config.py -q && mise run lint"
depends_on: [destroy-hook-input-output]
owns: [exit-policy]
uses: [ccw-world-fixture]
model: sonnet
estimate: 75
actual: ""
review: full
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Service exit policy

## Requirement

K8 exit policy (design D12). Owns part `exit-policy`.

- `sessions.exit_when_last_destroyed: bool = False`: when a DELETE or the idle reaper leaves no active session and at least one session was created successfully in this process, the service stops through its normal shutdown (hooks drained) and exits 0. Shutdown itself never re-triggers it.
- `sessions.exit_when_dead: bool = False`: a session reaching `dead` stops the service through its normal shutdown and the process exits 3. Before stopping it writes `session <id> dead: <last_fatal or "claude exited">` to `TERMINATION_LOG` (module constant `/dev/termination-log`) when that path is writable, and skips it silently otherwise.
- `SessionManager` takes `on_exit_request: Callable[[int], None] | None`. `service._run` wires it to record the code on `ServiceHandle.exit_code` and set `stop`; the first request wins. `run_service` leaves the code on the handle; `ccw serve` (cli `_serve`) returns it as the process exit status (0 when no request was made).
- Defaults off: nothing changes.
- README: both config rows, and the `## Running in Kubernetes` note (Job completion via exit_when_last_destroyed, termination message via exit_when_dead).

## Acceptance conditions

- AC1: deleting the last session stops the service with exit code 0 [evidence: test:tests/test_exit_policy.py::test_last_destroy_stops_service_exit_0]
- AC2: the idle reaper emptying the service also triggers the exit [evidence: test:tests/test_exit_policy.py::test_idle_reaper_emptying_triggers_exit]
- AC3: no exit before any session was created [evidence: test:tests/test_exit_policy.py::test_no_exit_before_any_session]
- AC4: a dead session exits 3 and writes the termination log [evidence: test:tests/test_exit_policy.py::test_dead_session_exits_3_and_writes_termination_log]
- AC5: the exit path still drains onDestroy hooks [evidence: test:tests/test_exit_policy.py::test_shutdown_drains_on_destroy_before_exit]
- AC6: ccw serve returns the requested exit code [evidence: test:tests/test_exit_policy.py::test_cli_serve_returns_exit_code]
- AC7: with defaults the service keeps running after the last delete [evidence: test:tests/test_exit_policy.py::test_defaults_keep_service_running]
- AC8: README documents both keys [evidence: command:uv run pytest tests/test_readme_config.py -q]

## Files

- `src/claude_code_wrapper/config.py`
- `src/claude_code_wrapper/manager.py`
- `src/claude_code_wrapper/service.py`
- `src/claude_code_wrapper/cli.py`
- `README.md`
- `tests/test_exit_policy.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
