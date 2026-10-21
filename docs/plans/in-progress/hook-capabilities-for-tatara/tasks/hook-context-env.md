---
title: Hook context env
status: in-progress
repo: .
value: "Hook scripts get a per-session state dir and the API URL, and the env docs tell the truth (K4, DEF-3)"
files: [src/claude_code_wrapper/hooks.py, src/claude_code_wrapper/manager.py, src/claude_code_wrapper/service.py, README.md, tests/test_hook_context_env.py]
verify: "uv run pytest tests/test_hook_context_env.py tests/test_hooks_command.py tests/test_manager.py -q && mise run lint"
depends_on: [pretooluse-gate-fails-closed, fake-claude-hook-scenarios]
owns: [hook-context-env]
uses: [ccw-world-fixture]
model: sonnet
estimate: 60
actual: ""
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/hook-capabilities-for-tatara--hook-context-env
branch: hook-capabilities-for-tatara--hook-context-env
verified_head: ""
run: ""
provides: []
---

# Hook context env

## Requirement

K4 hook context env (design D8) and DEF-3 env documentation (design D4).

- Every command action's environment gains `CCW_STATE_DIR` = `<session_dir>/hook-state` and `CCW_API_URL` = `http://127.0.0.1:<public port actually bound>`.
- `CCW_STATE_DIR` is created before the session's first hook fires (before onCreate), kept across stop and resume, and removed by purge. It is per session.
- `SessionManager` takes `api_url: str = ""`; `service._run` passes `http://127.0.0.1:<handle.public_port>`. `HookDispatcher` receives the state dir and api url (constructor params) and sets both vars after the action `env` (service-managed vars win). Empty strings when not configured (unit tests).
- In the hook env `CCW_CLAUDE_CONFIG_DIR` and the claude CLI `CLAUDE_CONFIG_DIR` of the same session resolve to the same path.
- Webhook payloads are unchanged.
- DEF-3: README `### Command actions` says hook command actions get the FULL service environment (every variable of the ccw process, including secrets such as `CCW_AUTH_TOKEN`), then the action's `env`, then `CCW_EVENT`, `CCW_SESSION_ID`, `CCW_TURN_ID`, `CCW_WORKDIR`, `CCW_CLAUDE_CONFIG_DIR`, `CCW_STATE_DIR`, `CCW_API_URL`; and that `init.setup_commands` get the allow-listed tool environment instead (base vars, passthrough, claude env, mise, `CLAUDE_CONFIG_DIR`, `GIT_CONFIG_GLOBAL`). The wrong "service's child environment" wording is removed. Hooks keep the full service env (no narrowing).

## Acceptance conditions

- AC1: CCW_STATE_DIR is set in every command action env and exists before onCreate runs [evidence: test:tests/test_hook_context_env.py::test_state_dir_in_env_and_created]
- AC2: the state dir and its files survive stop and resume [evidence: test:tests/test_hook_context_env.py::test_state_dir_survives_stop_and_resume]
- AC3: purge removes the state dir [evidence: test:tests/test_hook_context_env.py::test_state_dir_removed_on_purge]
- AC4: CCW_API_URL names the bound public port under run_service with port 0 [evidence: test:tests/test_hook_context_env.py::test_api_url_is_bound_public_port]
- AC5: a service env var outside passthrough reaches the hook (full service env) [evidence: test:tests/test_hook_context_env.py::test_hook_env_is_full_service_env]
- AC5a: CCW_CLAUDE_CONFIG_DIR in the hook env and CLAUDE_CONFIG_DIR of the same session resolve to the same path [evidence: test:tests/test_hook_context_env.py::test_hook_and_claude_config_dir_match]
- AC6: README drops the child-environment wording and documents the new vars [evidence: command:! grep -q "service's child environment" README.md && grep -q CCW_STATE_DIR README.md && grep -q CCW_API_URL README.md]

## Files

- `src/claude_code_wrapper/hooks.py`
- `src/claude_code_wrapper/manager.py`
- `src/claude_code_wrapper/service.py`
- `README.md`
- `tests/test_hook_context_env.py`

## Interfaces

- Part `hook-context-env`: `CCW_STATE_DIR`, `CCW_API_URL` in `hooks.py::HookDispatcher._command` env; `HookDispatcher(..., state_dir=..., api_url=...)`; `SessionManager(..., api_url="")`.

## Expected outcome

Hook scripts can keep per-session state and call back into ccw's API; the README describes the real hook and setup-command environments.

## Steps

- Write `tests/test_hook_context_env.py` (a command hook dumping its env to a file; `world` for the session cases, `run_service` with port 0 for the API URL); see it fail.
- Implement the dispatcher params, state dir lifecycle in the manager (create before onCreate, keep on stop/resume, delete on purge), the service wiring.
- Rewrite README `### Command actions` env paragraph and the setup_commands env note.
- `uv run pytest tests/test_hook_context_env.py tests/test_hooks_command.py tests/test_manager.py -q && mise run lint`.

## Notes

- Design D4: keep the full service env for hooks (finalisers need secrets that must not reach claude); fix only the doc.
- Facts: base_env=service_env (manager.py `_dispatcher`), service_env=dict(os.environ) (service.py `_run`), setup_commands use `_tool_env`.
