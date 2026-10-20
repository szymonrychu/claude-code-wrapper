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
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Internal API

## Requirement

Build the loopback internal API (D7, D9) in `src/claude_code_wrapper/internal_api.py`.

- `create_internal_app(manager) -> FastAPI` with:
  - `POST /internal/hooks/{event}`: `event` must be in `CLAUDE_HOOK_EVENTS` (else 404); the request client host must be a loopback address (else 403); `X-CCW-Session` must name an active session and `X-CCW-Hook-Token` must match its token in constant time (else 403); the body must be a JSON object (else 400). Delegates to `manager.router(session_id).handle_claude_hook(event, payload)` and returns its `RelayResponse` as JSON with status 200.
  - `GET /internal/healthz` -> `{"status": "ok"}`.
- No public route is mounted on this app and it never logs hook tokens.

## Acceptance conditions

- AC1: A hook POST is routed to the session router and its response returned [evidence: test:tests/test_internal_api.py::test_routes_hook_to_session_router]
- AC2: A wrong hook token is rejected with 403 [evidence: test:tests/test_internal_api.py::test_wrong_token_403]
- AC3: An unknown session is rejected with 403 [evidence: test:tests/test_internal_api.py::test_unknown_session_403]
- AC4: An unknown Claude event gives 404 [evidence: test:tests/test_internal_api.py::test_unknown_event_404]
- AC5: A non-loopback client is rejected [evidence: test:tests/test_internal_api.py::test_non_loopback_client_rejected]
- AC6: A real ccw-hook relay process gets a PreToolUse deny back through uvicorn [evidence: test:tests/test_internal_api.py::test_pretooluse_deny_round_trip_through_relay]
- AC7: A non-object JSON body gives 400 [evidence: test:tests/test_internal_api.py::test_bad_body_400]

## Files

- `src/claude_code_wrapper/internal_api.py`
- `tests/test_internal_api.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
