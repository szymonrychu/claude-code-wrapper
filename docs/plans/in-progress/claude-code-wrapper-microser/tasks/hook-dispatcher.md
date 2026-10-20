---
title: Hook dispatcher
status: in-progress
repo: claude-code-wrapper
value: User hooks as commands or signed webhooks that can veto safely
files: [src/claude_code_wrapper/hooks.py, tests/test_hooks_command.py, tests/test_hooks_webhook.py]
verify: "uv run pytest tests/test_hooks_command.py tests/test_hooks_webhook.py -q && mise run lint"
depends_on: [config-models]
owns: [hook-dispatcher]
uses: [config-schema, redactor]
model: ""
estimate: 90
actual: ""
review: full
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/claude-code-wrapper-microser--hook-dispatcher
branch: claude-code-wrapper-microser--hook-dispatcher
verified_head: ""
run: ""
provides: []
---

# Hook dispatcher

## Requirement

Build the user hook dispatcher (D6) in `src/claude_code_wrapper/hooks.py`.

- `build_payload(event, *, session_id, claude_session_id, turn_id, workdir, data) -> dict`: the envelope `{event, session_id, claude_session_id, turn_id, ts (ISO 8601 UTC), workdir, data}`.
- `HookDispatcher(hooks_cfg, redactor, *, base_env, default_cwd, config_dir=None, transport=None, on_result=None)`.
- `async fire(event, payload, *, tool_name=None, cwd=None) -> HookOutcome`: picks the actions configured for `event` whose `matcher` (regex full match) matches `tool_name` (actions without matcher always match). The payload is redacted with the redactor before it leaves the process. Blocking actions run concurrently and are awaited, each bounded by its timeout; non-blocking actions are started as tracked tasks and never delay the caller. `HookOutcome` has `decision` (`allow`/`deny`), `reason`, `additional_context` (blocking actions' contexts joined by blank lines) and `results: list[ActionResult]` (event, index, type, ok, decision, duration_ms, error). Deny wins; the first deny's reason is kept. For events not in VETO_EVENTS a deny is recorded in results but `decision` stays `allow`.
- Command action: `sh -c <run>` in its own process group, cwd = `cwd` or `default_cwd`, stdin = payload JSON, env = `base_env` + action `env` + `CCW_EVENT`, `CCW_SESSION_ID`, `CCW_TURN_ID`, `CCW_WORKDIR`, `CCW_CLAUDE_CONFIG_DIR` (from the payload and caller). Exit 0 = ok/allow; stdout that parses as a JSON object may carry `decision`, `reason`, `additional_context`. Exit 2 = deny with stripped stderr (capped at 2000 chars) as reason. Any other exit or a timeout (process group killed) = error: the action's `on_error` decides allow or deny for blocking actions; logged for non-blocking ones.
- Webhook action: POST the payload JSON via `httpx.AsyncClient` (injectable `transport` for tests) with headers `Content-Type: application/json`, `X-CCW-Event`, `X-CCW-Delivery` (uuid4), action `headers`, and `X-CCW-Signature: sha256=<hex HMAC-SHA256(secret, raw body)>` when `secret` is set. Retries (`retries`, 0.5 s doubling) on connection errors and non-2xx. A 2xx JSON object body may carry the same decision fields; an empty or non-JSON 2xx body = allow. Final failure = error handled by `on_error`.
- `on_result(ActionResult)` callback is called for every finished action (event-router emits `hook_result` events from it).
- `async post_json(url, body, *, secret=None, retries=3)`: the same signed delivery used for per-turn `callback_url`s.
- `async drain(timeout)`: awaits pending non-blocking tasks up to `timeout`, then cancels the rest.
- Nothing in the dispatcher raises to the caller; every failure becomes an ActionResult.

Settled by the builder check (binding):
- One dispatcher per session; constructor kwarg `config_dir` is exported to command actions as `CCW_CLAUDE_CONFIG_DIR`.

## Acceptance conditions

- AC1: A command action gets the payload on stdin and the CCW env vars [evidence: test:tests/test_hooks_command.py::test_command_receives_payload_on_stdin_and_env]
- AC2: Exit code 2 denies with stderr as the reason [evidence: test:tests/test_hooks_command.py::test_exit_2_denies_with_stderr_reason]
- AC3: Stdout JSON can set decision, reason and additional_context [evidence: test:tests/test_hooks_command.py::test_stdout_json_decision]
- AC4: A timeout kills the command and applies on_error [evidence: test:tests/test_hooks_command.py::test_timeout_kills_and_applies_on_error]
- AC5: Non-blocking actions do not delay fire [evidence: test:tests/test_hooks_command.py::test_non_blocking_does_not_delay]
- AC6: matcher filters by tool name [evidence: test:tests/test_hooks_command.py::test_matcher_filters_tool_name]
- AC7: Deny wins across concurrent blocking actions [evidence: test:tests/test_hooks_command.py::test_deny_wins_across_actions]
- AC8: Deny on a non-veto event does not change the decision [evidence: test:tests/test_hooks_command.py::test_deny_ignored_for_non_veto_events]
- AC9: drain waits for pending non-blocking actions [evidence: test:tests/test_hooks_command.py::test_drain_waits_pending]
- AC10: Webhooks carry a valid HMAC-SHA256 signature [evidence: test:tests/test_hooks_webhook.py::test_webhook_signature_hmac_sha256]
- AC11: Webhooks retry on 5xx then succeed [evidence: test:tests/test_hooks_webhook.py::test_webhook_retries_on_5xx]
- AC12: A blocking webhook can deny via its JSON response [evidence: test:tests/test_hooks_webhook.py::test_blocking_webhook_deny_response]
- AC13: A failing blocking webhook with on_error deny denies [evidence: test:tests/test_hooks_webhook.py::test_webhook_failure_on_error_deny]
- AC14: Secrets are redacted from payloads before delivery [evidence: test:tests/test_hooks_webhook.py::test_payload_redacted]
- AC15: on_result is called for every finished action [evidence: test:tests/test_hooks_webhook.py::test_on_result_called]

- AC16: config_dir reaches command actions as CCW_CLAUDE_CONFIG_DIR [evidence: test:tests/test_hooks_command.py::test_config_dir_env]

## Files

- `src/claude_code_wrapper/hooks.py`
- `tests/test_hooks_command.py`
- `tests/test_hooks_webhook.py`

## Interfaces

- Part `hook-dispatcher`: `claude_code_wrapper.hooks.HookDispatcher`, `HookOutcome`, `ActionResult`, `build_payload`, `post_json`. Consumers: event-router, session-manager (via the router), readme-api (payload schema and semantics).

## Expected outcome

Any config.yaml hook runs as a shell command or webhook with a documented payload, can veto where veto makes sense, and can never crash or hang the service.

## Steps

- Write `tests/test_hooks_command.py` (real `sh` commands writing to tmp files) and `tests/test_hooks_webhook.py` (`httpx.MockTransport`); red.
- Implement command actions, then webhooks; green; `mise run lint`.

## Notes

- Sources: tatara `W/internal/bootstrap/hooks_lifecycle.go:22-59` (best-effort runner), `W/internal/webhook/webhook.go` (HMAC, retries, drain); ros2 `R/tools.py:319-340` (failures become explicit decisions, never exceptions into claude).
- Exit 2 = deny mirrors Claude Code's own hook convention, which users already know.
