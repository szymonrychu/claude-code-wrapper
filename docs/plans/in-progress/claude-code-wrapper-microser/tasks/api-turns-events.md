---
title: API turns events
status: in-progress
repo: claude-code-wrapper
value: Clients drive turns and follow sessions by polling or SSE
files: [src/claude_code_wrapper/api/turns.py, src/claude_code_wrapper/api/events.py, src/claude_code_wrapper/api/app.py, tests/test_api_turns.py, tests/test_api_events.py]
verify: "uv run pytest tests/test_api_turns.py tests/test_api_events.py -q && mise run lint"
depends_on: [api-sessions]
owns: []
uses: [public-api, session-manager, event-log, prompt-templates]
model: ""
estimate: 90
actual: ""
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/claude-code-wrapper-microser--api-turns-events
branch: claude-code-wrapper-microser--api-turns-events
verified_head: ""
run: ""
provides: []
---

# API turns events

## Requirement

Add turn, control, transcript, history and SSE routes (D5, D14) in `src/claude_code_wrapper/api/turns.py` and `api/events.py`, registered in `api/app.py`.

turns.py:
- `POST /v1/sessions/{id}/turns` body `{text?, template?, variables?, files?: {path: content}, callbackUrl?}` -> 202 `{turn_id, state}`. Exactly one of text/template (else 400 `text_or_template`); template rendered via `resolve_prompt` (422 PromptError codes); prompt bytes over `limits.max_prompt_bytes` -> 413 `too_large`; file caps -> 413 `too_large`, bad path -> 400 `invalid_path`; `callbackUrl` must be http(s) (400); busy -> 409 `busy`; booting -> 503 `booting`; dead/stopped -> 503 `dead`; onTurn veto -> 403 `vetoed`. Delegates to `router.submit_turn`.
- `GET /v1/sessions/{id}/turns` -> `{"turns": [summary]}`; `GET /v1/sessions/{id}/turns/{turn_id}` -> full record (redacted); unknown -> 404.
- `POST /v1/sessions/{id}/interrupt` -> 200 `{interrupted: bool, turn_id}`.
- `POST /v1/sessions/{id}/probe` body `{text?}` -> 202 `{probe_id, state}`; `GET /v1/sessions/{id}/probe/{probe_id}` -> `{probe_id, state, created_at, delivered_at, answered_at}`.
- `GET /v1/sessions/{id}/transcript` -> `application/x-ndjson` streaming the main transcript file line by line, each line redacted; 404 `no_transcript` when the file does not exist yet.

events.py:
- `GET /v1/sessions/{id}/history?after_seq=0&limit=100` -> `{events: [...], next_seq, has_more}` (limit capped at 1000).
- `GET /v1/sessions/{id}/events` -> `text/event-stream`. Replay start: the `Last-Event-ID` header, else the `after_seq` query, else the last `limits.sse_replay` events. Then live events from `EventLog.subscribe()`, de-duplicated by seq against the replay. Frame: `id: <seq>\nevent: <type>\ndata: <json envelope>\n\n`. A `: ping` comment every `limits.sse_heartbeat_seconds`. When the session stops (or is already stopped) the stream sends `session_closed` and ends. Headers `Cache-Control: no-cache` and `X-Accel-Buffering: no`.
- All routes use the auth dependency and the error envelope from api-sessions.

Settled by the builder check (binding):
- History, turns listing/fetch and SSE use `manager.open_event_log(id)` / `manager.open_turns(id)`, so they work for stopped sessions from disk; SSE on a stopped session replays and then sends `session_closed`.
- `text_or_template` is 400 (shared mapping from api/errors.py).

Settled at the gate (D19-D24, binding):
- The turn body field is `callbackUrl` (camelCase); `callback_url` is rejected as an unknown field (422).
- No callback host policy (trust model D23).

## Acceptance conditions

- AC1: Submitting a turn returns 202 with a turn id [evidence: test:tests/test_api_turns.py::test_submit_turn_202]
- AC2: Body validation maps to 400 and size caps to 413 [evidence: test:tests/test_api_turns.py::test_submit_validation_400_413]
- AC3: Busy maps to 409 and booting or dead to 503 [evidence: test:tests/test_api_turns.py::test_submit_busy_409_and_booting_503]
- AC4: A template turn is rendered with variables [evidence: test:tests/test_api_turns.py::test_template_turn_rendered]
- AC5: An onTurn veto maps to 403 vetoed [evidence: test:tests/test_api_turns.py::test_submit_vetoed_403]
- AC6: Turns can be listed and fetched [evidence: test:tests/test_api_turns.py::test_list_and_get_turn]
- AC7: The interrupt endpoint reports what it interrupted [evidence: test:tests/test_api_turns.py::test_interrupt_endpoint]
- AC8: Probe endpoints create and report probes [evidence: test:tests/test_api_turns.py::test_probe_endpoints]
- AC9: The transcript streams as redacted NDJSON [evidence: test:tests/test_api_turns.py::test_transcript_ndjson_redacted]
- AC10: History pages by after_seq and limit [evidence: test:tests/test_api_events.py::test_history_paging]
- AC11: SSE replays from Last-Event-ID [evidence: test:tests/test_api_events.py::test_sse_replay_from_last_event_id]
- AC12: SSE streams live events without duplicates and sends heartbeats [evidence: test:tests/test_api_events.py::test_sse_live_events_and_heartbeat]
- AC13: SSE ends with session_closed when the session stops [evidence: test:tests/test_api_events.py::test_sse_closes_when_session_stops]

- AC14: History and turns are served for a stopped session from disk [evidence: test:tests/test_api_events.py::test_history_and_turns_for_stopped_session]

- AC15: The turn body uses camelCase callbackUrl [evidence: test:tests/test_api_turns.py::test_turn_body_uses_callback_url_camel_case]

## Files

- `src/claude_code_wrapper/api/turns.py`
- `src/claude_code_wrapper/api/events.py`
- `src/claude_code_wrapper/api/app.py`
- `tests/test_api_turns.py`
- `tests/test_api_events.py`

## Interfaces

- Routers in `claude_code_wrapper.api.turns` and `claude_code_wrapper.api.events`, mounted by `create_app`. Uses `EventLog.page/subscribe`, `SessionRouter.submit_turn/interrupt`, `Session.probe/get_probe`, `resolve_prompt`.

## Expected outcome

A client can drive turns and follow everything a session does either by polling or by one SSE connection that survives reconnects without gaps or duplicates.

## Steps

- Write `tests/test_api_turns.py` and `tests/test_api_events.py` with ASGITransport, a real EventLog and router/session doubles; red.
- Implement; green; `mise run lint`.

## Notes

- Source: ros2 history-on-connect plus seq de-dup (`R/api.py:170-189`); 202-then-events (`R/api.py:141-156`); tatara `GET /v1/transcript` NDJSON.
- Small heartbeat values in tests (0.1 s) keep the suite fast.
