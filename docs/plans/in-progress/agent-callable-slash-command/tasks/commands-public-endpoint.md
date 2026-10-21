---
title: Commands public endpoint
status: in-progress
repo: .
value: API clients can queue and read slash commands for a session without typing into it
files: [src/claude_code_wrapper/api/commands.py, src/claude_code_wrapper/api/app.py, src/claude_code_wrapper/api/errors.py, README.md, tests/test_api_commands.py]
verify: "uv run pytest tests/test_api_commands.py tests/test_api_turns.py tests/test_api_sessions.py tests/test_metric_families.py tests/test_readme_api.py -q && mise run lint"
depends_on: [command-same-turn-resume]
owns: []
uses: [command-request-api, command-queue, commands-config]
model: sonnet
estimate: 60
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Commands public endpoint

## Requirement
Public commands endpoint for clients (design D5; user decision, redundancy with the turns endpoint accepted).

- `POST /v1/sessions/{id}/commands` with body `{name, args?}` (extra forbid; `args` an object, default `{}`) -> 202 with the CommandRecord JSON; origin `client`, so the session's client allowlist applies; same auth as every `/v1` route.
- Error mapping of `CommandRejected` (my call, design D5): `not_allowed` -> 403 `command_not_allowed`; `invalid_params` and `force_not_allowed` -> 422 `invalid_command`; `queue_full` -> 429 `command_queue_full`; `invalid_state` -> 409 with the code the turns endpoint uses for a session that is not running; unknown session -> 404 as today.
- `GET /v1/sessions/{id}/commands` -> `{"commands": [records, oldest first]}`; `GET /v1/sessions/{id}/commands/{command_id}` -> the record, or 404 `command_not_found`.
- Same queue and lifecycle as the agent tool: on an idle session the command runs at once; during a turn it waits for the Stop and the turn resumes (task command-same-turn-resume); the record read back shows its final state.
- `api/app.py` includes the new router on `/v1`; the http metrics route template covers the new routes.
- README: `### POST /v1/sessions/{id}/commands`, `### GET /v1/sessions/{id}/commands`, `### GET /v1/sessions/{id}/commands/{command_id}`, and the four error codes in `## Errors` (tests/test_readme_api.py requires routes and error codes).

## Acceptance conditions

- AC1: POST commands returns 202 with the record and the command runs on an idle session [evidence: test:tests/test_api_commands.py::test_post_command_returns_202_record]
- AC2: a name outside the client allowlist returns 403 command_not_allowed [evidence: test:tests/test_api_commands.py::test_post_command_not_allowed_403]
- AC3: bad args return 422 invalid_command [evidence: test:tests/test_api_commands.py::test_post_command_bad_args_422]
- AC4: a full queue returns 429 command_queue_full [evidence: test:tests/test_api_commands.py::test_queue_full_429]
- AC5: GET returns one record and the list oldest first [evidence: test:tests/test_api_commands.py::test_get_command_and_list]
- AC6: an unknown command id returns 404 command_not_found [evidence: test:tests/test_api_commands.py::test_unknown_command_id_404]
- AC7: the routes require the API token [evidence: test:tests/test_api_commands.py::test_commands_require_auth]
- AC8: a command posted during a turn runs at its Stop and the turn resumes [evidence: test:tests/test_api_commands.py::test_client_command_during_turn_resumes_turn]
- AC9: README documents the routes and error codes, and existing API tests stay green [evidence: command:uv run pytest tests/test_readme_api.py tests/test_api_turns.py tests/test_api_sessions.py tests/test_metric_families.py -q]

## Files

- `src/claude_code_wrapper/api/commands.py`
- `src/claude_code_wrapper/api/app.py`
- `src/claude_code_wrapper/api/errors.py`
- `README.md`
- `tests/test_api_commands.py`

## Interfaces

`api/commands.py` router; error codes `command_not_allowed`, `invalid_command`, `command_queue_full`, `command_not_found`.

## Expected outcome

API clients queue, list and read commands per session through `/v1/sessions/{id}/commands`, with documented error codes, on the same lifecycle as the agent tool.

## Steps

- Write tests/test_api_commands.py with the existing API test client pattern of tests/test_api_turns.py; run it and see it fail.
- Add api/commands.py, include it in api/app.py, add the error codes in api/errors.py, document routes and codes in README.
- Run `uv run pytest tests/test_api_commands.py tests/test_api_turns.py tests/test_api_sessions.py tests/test_metric_families.py tests/test_readme_api.py -q && mise run lint`.
- A retirement test, if any, asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Design D-numbers and facts with file:line are in ../design.md.
