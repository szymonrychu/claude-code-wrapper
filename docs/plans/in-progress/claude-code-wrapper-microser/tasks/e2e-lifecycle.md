---
title: E2E lifecycle
status: in-progress
repo: claude-code-wrapper
value: Proof the whole lifecycle works end to end without real Claude
files: [tests/test_e2e_lifecycle.py, tests/test_e2e_resume.py, tests/fixtures/scenarios/e2e.json, tests/fixtures/hooks/record_hook.sh]
verify: "uv run pytest tests/test_e2e_lifecycle.py tests/test_e2e_resume.py -q && mise run lint"
depends_on: [service-main]
owns: []
uses: [service-cli, fake-claude]
model: opus
estimate: 90
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# E2E lifecycle

## Requirement

Prove the whole service end to end against the fake claude (D15): `tests/test_e2e_lifecycle.py`, `tests/test_e2e_resume.py`, scenario `tests/fixtures/scenarios/e2e.json`, hook script `tests/fixtures/hooks/record_hook.sh`.

Harness: `run_service` in the test loop on port 0 with a tmp config: `claude.binary` = the `fake_claude_bin` shim, `paths` under tmp, fast boot settings, `server.auth_token` set, `claude.env.claude_code_oauth_token` set to a known fake secret, and EVERY hook event configured with a command action running `record_hook.sh` (appends `$CCW_EVENT` and the stdin payload as one JSON line to a log file; exits 2 with a reason on stderr when the event is onAgentToolCall and the payload's tool_name is `Write`; that action is `blocking: true`). onTurnComplete also has a webhook action to a local receiver (uvicorn on 127.0.0.1:0 in the test) with a `secret`. All requests go over real HTTP with httpx to the public port; claude's hooks travel through the real `ccw-hook` relay to the real internal port.

Scenarios (one test each):
1. Lifecycle: POST /v1/sessions -> 201 ready; open SSE; POST a turn matching the e2e scenario (thinking, text, Bash tool allowed, Write tool denied, a subagent with text, final text); poll GET turn until completed. Assert the SSE stream contains, in order: turn_started, tool_call (Bash), tool_result (Bash), tool_call (Write), tool_denied (Write), subagent_started, subagent_stopped, turn_completed, with assistant_text events for the main text; assert the hook log has onCreate, onTurn, onAgentToolCall (Bash and Write), onToolResult, onSubagentStart, onSubagentStop, onAgentResponse, onTurnComplete with matching session_id and turn_id; assert the webhook received onTurnComplete with a valid `X-CCW-Signature`; assert GET transcript returns NDJSON lines; DELETE -> onDestroy logged and state stopped.
2. Interrupt: a turn whose scenario sleeps; POST interrupt; the turn ends `interrupted`; onInterrupt logged; a new turn is accepted afterwards.
3. Fatal: a turn whose scenario prints `API Error: 401`; the turn fails with that error; onError logged; the session accepts the next turn.
4. AskUserQuestion: a turn whose scenario calls AskUserQuestion; a `question` SSE event carries the questions; the turn completes.
5. Resume after restart (test_e2e_resume.py): create, one turn, stop the service; start a NEW service on the same sessions_root; GET /v1/sessions lists the session as stopped; POST resume -> ready; FAKE_CLAUDE_LOG shows `--resume <claude_session_id>`; onResume logged; a second turn appends to the same transcript file.
6. Purge (test_e2e_resume.py): DELETE ?purge=true removes the session dir and the empty-mode workdir.
7. Secrets: across every response body, SSE frame, hook payload log line and session.json, the fake OAuth token and the hook token never appear.

Settled by the builder check (binding):
- `FAKE_CLAUDE_SCENARIO` and `FAKE_CLAUDE_LOG` reach the fake via `claude.env.extra` (or `claude.passthrough_env`); ports come from `ServiceHandle` with both ports 0.
- create returns before the initial turn; tests poll for turn completion.

Settled at the gate (D19-D24, binding):
- The AskUserQuestion scenario sets a turn `callbackUrl` pointing at the test receiver and asserts the `question` envelope arrives there signed.
- The interrupt and fatal scenarios assert onTurnComplete fired with `state` interrupted and failed.

## Acceptance conditions

- AC1: The full lifecycle emits the expected SSE events and fires every turn-related hook [evidence: test:tests/test_e2e_lifecycle.py::test_full_lifecycle_events_and_hooks]
- AC2: A blocking onAgentToolCall hook denies the Write tool end to end [evidence: test:tests/test_e2e_lifecycle.py::test_tool_gate_denies_write]
- AC3: The onTurnComplete webhook arrives with a valid HMAC signature [evidence: test:tests/test_e2e_lifecycle.py::test_webhook_signed]
- AC4: Interrupt ends the turn and the session accepts the next one [evidence: test:tests/test_e2e_lifecycle.py::test_interrupt_flow]
- AC5: A fatal screen error fails the turn and fires onError [evidence: test:tests/test_e2e_lifecycle.py::test_fatal_error_flow]
- AC6: AskUserQuestion becomes a question event [evidence: test:tests/test_e2e_lifecycle.py::test_ask_user_question_forwarded]
- AC7: A session is resumed by id after a service restart with --resume [evidence: test:tests/test_e2e_resume.py::test_resume_after_service_restart]
- AC8: Purge deletes the session dir and the empty-mode workdir [evidence: test:tests/test_e2e_resume.py::test_purge_removes_files]
- AC9: No secret appears in responses, SSE, hook payloads or session.json [evidence: test:tests/test_e2e_lifecycle.py::test_no_secret_leaks]
- AC10: The full suite passes [evidence: command:mise run test]

- AC11: The question event is POSTed to the turn callbackUrl [evidence: test:tests/test_e2e_lifecycle.py::test_question_posted_to_callback]
- AC12: onTurnComplete fires for interrupted and failed turns with their state [evidence: test:tests/test_e2e_lifecycle.py::test_on_turn_complete_for_terminal_states]

## Files

- `tests/test_e2e_lifecycle.py`
- `tests/test_e2e_resume.py`
- `tests/fixtures/scenarios/e2e.json`
- `tests/fixtures/hooks/record_hook.sh`

## Interfaces

- Test-only; uses `run_service` (part `service-cli`) and the fake claude (part `fake-claude`).

## Expected outcome

One command proves create -> turn -> response -> tool call -> gate -> hooks -> SSE -> interrupt -> destroy -> resume works across every real component, without real Claude.

## Steps

- Write the scenario, the hook script and the tests; run `uv run pytest tests/test_e2e_lifecycle.py tests/test_e2e_resume.py -q`; a failure in a production module outside this task's files is returned as `discovered` (module, failing scenario, observed vs expected) so the run adds a fix task; failures in the test harness itself are fixed here.
- `mise run test` and `mise run lint`.

## Notes

- The relay must be the venv's `ccw-hook` (settings-render resolves it next to `sys.executable`).
- Bound every wait (poll with deadlines of a few seconds); a hung test is a failure, not a slow pass.
- This task changes only its four files; production defects it finds go to `discovered`.
