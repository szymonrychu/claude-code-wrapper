---
title: Internal API
status: todo
repo: claude-code-wrapper
value: "Only the service's own claude processes can drive session hooks"
files: [src/claude_code_wrapper/internal_api.py, tests/test_internal_api.py]
verify: "uv run pytest tests/test_internal_api.py -q && mise run lint"
depends_on: [session-manager, hook-relay]
owns: [internal-api]
uses: [session-manager, hook-relay-protocol]
model: ""
estimate: 60
actual: ""
review: full
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/claude-code-wrapper-microser--internal-api
branch: claude-code-wrapper-microser--internal-api
verified_head: ""
run: claude-code-wrapper-microser
provides: []
acs_for_audit: []
process_gaps: []
---

# Internal API

## Requirement

Build the loopback internal API (D7, D9) in `src/claude_code_wrapper/internal_api.py`.

- `create_internal_app(manager) -> FastAPI` with:
  - `POST /internal/hooks/{event}`: `event` must be in `CLAUDE_HOOK_EVENTS` (else 404); the request client host must be a loopback address (else 403); `X-CCW-Session` must name an active session and `X-CCW-Hook-Token` must match its token in constant time (else 403); the body must be a JSON object (else 400). Delegates to `manager.router(session_id).handle_claude_hook(event, payload)` and returns its `RelayResponse` as JSON with status 200.
  - `GET /internal/healthz` -> `{"status": "ok"}`.
- No public route is mounted on this app and it never logs hook tokens.

Settled by the builder check (binding):
- `docs_url`, `redoc_url` and `openapi_url` are None.
- Dedupe: a bounded per-session cache (last 256 `X-CCW-Delivery` ids) maps a delivery id to its in-flight or finished result; a retry with a known id awaits or returns the same RelayResponse and never runs the router twice. A request without the header is handled normally.

## Acceptance conditions

- AC1: A hook POST is routed to the session router and its response returned [evidence: test:tests/test_internal_api.py::test_routes_hook_to_session_router]
- AC2: A wrong hook token is rejected with 403 [evidence: test:tests/test_internal_api.py::test_wrong_token_403]
- AC3: An unknown session is rejected with 403 [evidence: test:tests/test_internal_api.py::test_unknown_session_403]
- AC4: An unknown Claude event gives 404 [evidence: test:tests/test_internal_api.py::test_unknown_event_404]
- AC5: A non-loopback client is rejected [evidence: test:tests/test_internal_api.py::test_non_loopback_client_rejected]
- AC6: A real ccw-hook relay process gets a PreToolUse deny back through uvicorn [evidence: test:tests/test_internal_api.py::test_pretooluse_deny_round_trip_through_relay]
- AC7: A non-object JSON body gives 400 [evidence: test:tests/test_internal_api.py::test_bad_body_400]

- AC8: A retried delivery id runs the router once and returns the same response [evidence: test:tests/test_internal_api.py::test_duplicate_delivery_deduped]
- AC9: The internal app exposes no OpenAPI or docs routes [evidence: test:tests/test_internal_api.py::test_no_openapi_routes]

## Files

- `src/claude_code_wrapper/internal_api.py`
- `tests/test_internal_api.py`

## Interfaces

- Part `internal-api`: `claude_code_wrapper.internal_api.create_internal_app`. Consumer: service-main (binds it on 127.0.0.1).

## Expected outcome

Only the claude processes the service started (holding their session token, on loopback) can drive session hooks.

## Steps

- Write `tests/test_internal_api.py` with `httpx.ASGITransport` and a manager double; the round-trip test runs uvicorn on 127.0.0.1:0 in the test loop and the real relay via `[sys.executable, "-m", "claude_code_wrapper.hook_relay", "PreToolUse"]`; red; implement; green; `mise run lint`.

## Notes

- Sources: magic's separate loopback app so a bad bind cannot expose the hook route (`M/api.py:1-7, 317-325`); tatara `/internal/turn-complete` on 127.0.0.1:8090.
- `ASGITransport` lets you set the client address for the loopback test.

- 2026-10-20 run claude-code-wrapper-microser: dependency-failed on session-manager
