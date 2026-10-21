---
title: Service exit policy
status: in-progress
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
- With `exit_when_last_destroyed`, the triggering DELETE still returns 200 with its full body (including `hook_outputs` when present) before the process exits 0: the stop is scheduled after the response is sent, not inline in the handler.
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
- AC7a: with exit_when_last_destroyed the DELETE returns 200 with its full body, hook_outputs included, before the process exits 0 [evidence: test:tests/test_exit_policy.py::test_last_destroy_delete_response_complete_before_exit]
- AC8: README documents both keys [evidence: command:uv run pytest tests/test_readme_config.py -q]

## Files

- `src/claude_code_wrapper/config.py`
- `src/claude_code_wrapper/manager.py`
- `src/claude_code_wrapper/service.py`
- `src/claude_code_wrapper/cli.py`
- `README.md`
- `tests/test_exit_policy.py`

## Interfaces

- Part `exit-policy`: `SessionsConfig.exit_when_last_destroyed`, `SessionsConfig.exit_when_dead`, `SessionManager(on_exit_request=...)`, `ServiceHandle.exit_code`, `service.TERMINATION_LOG`, cli `_serve` return code.

## Expected outcome

`ccw serve` exits 0 after its last session is deleted, or 3 with a termination message when a session dies, when configured; otherwise it keeps running.

## Steps

- Write `tests/test_exit_policy.py` (`run_service` with port 0, the fake claude, monkeypatched `TERMINATION_LOG`); see it fail.
- Manager trigger points (destroy, reaper, dead state), service stop wiring, cli exit code.
- README rows and Kubernetes note.
- `uv run pytest tests/test_exit_policy.py tests/test_service.py tests/test_cli.py tests/test_readme_config.py -q && mise run lint`.

## Notes

- Design D12: not a hook (`kill -TERM $PPID` gives the wrong code, no termination message and races the dispatcher drain).
- Shutdown must not re-enter the trigger; the first exit request wins.
