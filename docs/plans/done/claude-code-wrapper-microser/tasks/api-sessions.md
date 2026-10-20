---
title: API sessions
status: done
repo: claude-code-wrapper
value: Clients manage sessions over an authenticated HTTP API
files: [src/claude_code_wrapper/api/__init__.py, src/claude_code_wrapper/api/app.py, src/claude_code_wrapper/api/auth.py, src/claude_code_wrapper/api/errors.py, src/claude_code_wrapper/api/sessions.py, tests/test_api_sessions.py, tests/test_api_auth.py]
verify: "uv run pytest tests/test_api_sessions.py tests/test_api_auth.py -q && mise run lint"
depends_on: [session-manager]
owns: [public-api]
uses: [session-manager, claude-env, redactor, prompt-templates]
model: ""
estimate: 90
actual: ""
review: full
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/claude-code-wrapper-microser--api-sessions
branch: claude-code-wrapper-microser--api-sessions
verified_head: c053cbe348a2312732ef5947a81a7f7310afd6cd
run: claude-code-wrapper-microser
provides: [api-error-envelope, api-bearer-auth]
acs_for_audit: []
process_gaps: []
---

# API sessions

## Requirement

Build the public API app, auth, error envelope and session routes (D4, D9, D2) under `src/claude_code_wrapper/api/`.

- `api/app.py` `create_app(manager, cfg) -> FastAPI` (part `public-api`): includes the routers of this package, installs the exception handlers, and exposes `GET /healthz` (always 200 `{"status": "ok"}`) and `GET /readyz` (200 `{"status": "ready"}` once `manager.started`, else 503). Later tasks add routers here.
- `api/auth.py`: dependency requiring `Authorization: Bearer <server.auth_token>` (constant-time compare) on every `/v1` route; missing or wrong -> 401 with `WWW-Authenticate: Bearer` and the error envelope; an empty `auth_token` disables auth (a warning is logged once at app creation). `/healthz`, `/readyz` and `/metrics` stay open.
- `api/errors.py`: error envelope `{"error": {"code": str, "message": str}}` and handlers: `SessionNotFound` 404 `not_found`; `TooManySessions` 429 `too_many_sessions`; `CreateVetoed` and `ResumeVetoed` 403 `vetoed`; `InvalidState` 409 `invalid_state`; `ResumeUnavailable` 409 `not_resumable`; `CreateFailed` 500 `create_failed` (message includes the step); `PromptError` 422 with its code; request validation errors 422 `invalid_request` with dotted field paths. Every message passes through the redactor.
- `api/sessions.py` routes:
  - `POST /v1/sessions` body `CreateSessionRequest` (name, workdir, model, effort, env, prompt, template, variables); `env` is validated as a partial `ClaudeEnv` (unknown var or managed var -> 422 naming it) -> 201 snapshot.
  - `GET /v1/sessions` (optional `state` filter) -> `{"sessions": [snapshot]}`.
  - `GET /v1/sessions/{id}` -> snapshot: `session_id, claude_session_id, name, state, workdir, workdir_mode, model, effort, created_at, last_activity_at, turns_completed, current_turn_id, stall_suspected_since, last_fatal, restarts, env` (env as `redacted()` of the effective merged env).
  - `DELETE /v1/sessions/{id}?purge=false` -> 200 snapshot (state stopped; purged sessions return the last snapshot with `purged: true`).
  - `POST /v1/sessions/{id}/resume` body `{env?}` -> 200 snapshot.
  - `GET /v1/config` -> `cfg.redacted()`.
  - `GET /v1/prompts` -> `{"templates": list_templates(cfg)}`.
- No response body anywhere contains a secret value (OAuth token, API key, hook token, webhook or callback secret, auth token).

Settled by the builder check (binding):
- `create_app` passes `docs_url=None, redoc_url=None, openapi_url=None`.
- `PromptError("text_or_template")` maps to 400 `text_or_template` everywhere; other PromptError codes stay 422.
- The `state` filter accepts only `creating, ready, busy, stopped, failed, dead` (else 422).
- `/readyz` reads `manager.started`.
- The `metrics=None` kwarg on `create_app` is added by the metrics task.

Settled at the gate (D19-D24, binding):
- `POST /v1/sessions` accepts `callbackUrl` (http or https, else 400); snapshots expose `callbackUrl`. camelCase `callbackUrl` is the only spelling in the API.
- `DELETE` without `purge` keeps the session dir and workdir.
- `POST /v1/sessions/{id}/resume` body `{env?}` is where secret overrides are resent.
- Trust model (D23): bearer-token callers are fully trusted; no workdir allowlist and no callback host policy; `workdir` in the create body is used as given.

## Acceptance conditions

- AC1: POST /v1/sessions returns 201 with a snapshot [evidence: test:tests/test_api_sessions.py::test_create_returns_201_snapshot]
- AC2: An invalid env override is rejected with 422 naming the variable [evidence: test:tests/test_api_sessions.py::test_create_env_override_validated_422]
- AC3: Sessions can be listed, filtered by state and fetched [evidence: test:tests/test_api_sessions.py::test_list_and_get]
- AC4: An unknown session gives the 404 envelope [evidence: test:tests/test_api_sessions.py::test_get_unknown_404]
- AC5: DELETE stops and purge=true purges [evidence: test:tests/test_api_sessions.py::test_delete_and_purge]
- AC6: POST resume returns the resumed snapshot and 409 when not resumable [evidence: test:tests/test_api_sessions.py::test_resume_endpoint]
- AC7: max_sessions maps to 429 [evidence: test:tests/test_api_sessions.py::test_too_many_sessions_429]
- AC8: An onCreate veto maps to 403 vetoed [evidence: test:tests/test_api_sessions.py::test_veto_403]
- AC9: GET /v1/config is redacted [evidence: test:tests/test_api_sessions.py::test_config_redacted]
- AC10: GET /v1/prompts lists templates [evidence: test:tests/test_api_sessions.py::test_prompts_listed]
- AC11: No response contains a configured secret value [evidence: test:tests/test_api_sessions.py::test_secrets_never_in_responses]
- AC12: A missing bearer token gives 401 [evidence: test:tests/test_api_auth.py::test_missing_token_401]
- AC13: A wrong bearer token gives 401 [evidence: test:tests/test_api_auth.py::test_wrong_token_401]
- AC14: Health and readiness are unauthenticated and readyz reflects manager start [evidence: test:tests/test_api_auth.py::test_health_unauthenticated]
- AC15: An empty auth_token leaves the API open [evidence: test:tests/test_api_auth.py::test_open_mode_when_no_token]

- AC16: The public app exposes no OpenAPI or docs routes [evidence: test:tests/test_api_auth.py::test_no_openapi_routes]
- AC17: text_or_template maps to 400 [evidence: test:tests/test_api_sessions.py::test_text_or_template_400]

- AC18: Create accepts camelCase callbackUrl and the snapshot returns it [evidence: test:tests/test_api_sessions.py::test_create_accepts_callback_url_camel_case]
- AC19: DELETE without purge keeps the session files [evidence: test:tests/test_api_sessions.py::test_delete_default_keeps_files]

## Files

- `src/claude_code_wrapper/api/__init__.py`
- `src/claude_code_wrapper/api/app.py`
- `src/claude_code_wrapper/api/auth.py`
- `src/claude_code_wrapper/api/errors.py`
- `src/claude_code_wrapper/api/sessions.py`
- `tests/test_api_sessions.py`
- `tests/test_api_auth.py`

## Interfaces

- Part `public-api`: `claude_code_wrapper.api.app.create_app(manager, cfg)`; `api.errors` envelope helpers; `api.auth.require_auth`. Consumers: api-turns-events (adds routers), metrics (adds /metrics), service-main (serves it), readme-api (route walk).

## Expected outcome

A client with the bearer token can create, inspect, list, destroy and resume sessions with predictable status codes and an error envelope, and never sees a secret.

## Steps

- Write `tests/test_api_auth.py` and `tests/test_api_sessions.py` with `httpx.AsyncClient(transport=ASGITransport(app))` and a manager double; red.
- Implement app, auth, errors, sessions; green; `mise run lint`.

## Notes

- Keep handlers thin: all behaviour lives in the manager.
- Snapshot field names are the README contract; keep them snake_case as listed.

- 2026-10-20 run claude-code-wrapper-microser: dependency-failed on session-manager
