---
title: README API
status: todo
repo: claude-code-wrapper
value: Client authors can integrate from the README alone
files: [README.md, tests/test_readme_api.py]
verify: "uv run pytest tests/test_readme_api.py -q && mise run lint"
depends_on: [e2e-lifecycle]
owns: []
uses: [public-api, event-log, hook-dispatcher, internal-api]
model: ""
estimate: 75
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# README API

## Requirement

Write the API half of `README.md` (D16) plus `tests/test_readme_api.py`, which makes undocumented routes, events and hooks fail the suite.

README sections (exact H2 headings): `## Overview` (what the service is, the PTY approach in one paragraph, the session model), `## Architecture` (the ASCII diagram from design.md and the module list), `## API reference`, `## Events (SSE)`, `## Hooks`, `## Errors`.
- API reference: one H3 per route, heading text exactly `` `METHOD /path` `` (for example `` `POST /v1/sessions/{id}/turns` ``), covering every route of `create_app` and of `create_internal_app`; each with auth, path/query parameters, request body fields (name, type, required, meaning), response body with an example JSON, and every status code with its error code.
- Events: the envelope, then one H3 per type in `EVENT_TYPES` (heading `` `<type>` ``) with its `data` fields, when it is emitted, and an example frame; the SSE replay rules (Last-Event-ID, after_seq, default replay, heartbeat, session_closed) and a curl example.
- Hooks: config shape, one H3 per name in `HOOK_EVENTS` (heading `` `<event>` ``) with trigger source, blocking/veto semantics and the `data` schema; the payload envelope; command actions (stdin, env vars, exit codes, stdout JSON); webhook actions (headers, signature verification examples in Python and shell, retries); how Claude hooks reach the service (relay, loopback, tokens); note that a plugin hook rewriting Bash via `updatedInput` means the executed command can differ from `tool_input`.
- Errors: the envelope and a table of every error code with its status.
Tests (read README.md, build the apps with stub managers): `test_every_public_route_documented`, `test_every_internal_route_documented`, `test_every_event_type_documented`, `test_every_hook_event_documented`, `test_error_codes_documented` (every code raised in `api/errors.py` and the route modules appears in the Errors table).

## Acceptance conditions

- AC1: Every public route has its own README heading [evidence: test:tests/test_readme_api.py::test_every_public_route_documented]
- AC2: Every internal route has its own README heading [evidence: test:tests/test_readme_api.py::test_every_internal_route_documented]
- AC3: Every SSE event type is documented [evidence: test:tests/test_readme_api.py::test_every_event_type_documented]
- AC4: Every hook event is documented [evidence: test:tests/test_readme_api.py::test_every_hook_event_documented]
- AC5: Every error code is in the Errors table [evidence: test:tests/test_readme_api.py::test_error_codes_documented]
- AC6: The API reference section exists [evidence: doc-section:README.md#API reference]
- AC7: The hooks section exists [evidence: doc-section:README.md#Hooks]
- AC8: Route docs give request and response bodies with examples and status codes accurate to the implementation [evidence: judgement:]

## Files

- `README.md`
- `tests/test_readme_api.py`

## Interfaces

- Reads `create_app`, `create_internal_app`, `EVENT_TYPES`, `HOOK_EVENTS`, the error handlers; writes only README.md and its test.

## Expected outcome

A client author can integrate with the service from the README alone, and adding a route, event or hook without documenting it turns the suite red.

## Steps

- Write `tests/test_readme_api.py`; red (README absent); write the sections; green; `mise run lint`.

## Notes

- Use the real snapshot, turn record and event field names from the code, not from memory; run the e2e test with logging to capture example payloads.
- readme-config adds the configuration, docker and workbench sections afterwards; leave room, do not write those.
