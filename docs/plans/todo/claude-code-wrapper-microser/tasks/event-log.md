---
title: Event log
status: todo
repo: claude-code-wrapper
value: "Durable, pageable, streamable session history"
files: [src/claude_code_wrapper/events.py, tests/test_event_log.py]
verify: "uv run pytest tests/test_event_log.py -q && mise run lint"
depends_on: [repo-scaffold]
owns: [event-log]
uses: []
model: ""
estimate: 60
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Event log

## Requirement

Build the per-session event log (D5) in `src/claude_code_wrapper/events.py`.

- `EVENT_TYPES`: exactly the design.md "Event types" list: session_state, turn_started, turn_rejected, turn_completed, turn_failed, assistant_text, thinking, tool_call, tool_result, tool_denied, question, subagent_started, subagent_stopped, interrupted, stall_suspected, probe, hook_result, error, session_closed.
- `Event` (frozen dataclass or pydantic): `seq: int`, `ts: str` (ISO 8601 UTC with milliseconds), `session_id: str`, `type: str`, `data: dict`; `to_json()`.
- `EventLog(path, session_id, ring_size=1000, max_bytes=52428800)`:
  - On open, reads the last valid line of an existing file and continues `seq` from it (seq never repeats across restarts); a corrupt trailing line is ignored.
  - `append(type, data) -> Event`: rejects unknown types (ValueError); assigns the next seq; appends one JSON line and flushes; keeps the event in a RAM ring of `ring_size`; publishes it to subscribers. A write failure is logged once (not per event) and never raises.
  - Rotation: when the file exceeds `max_bytes` after an append, rewrite it atomically keeping the newest half of its lines.
  - `page(after_seq=0, limit=100) -> (events, has_more)`: events with seq > after_seq in order, `limit` capped at 1000; served from the ring when it covers `after_seq`, else read from the file.
  - `last(n) -> list[Event]`.
  - `subscribe() -> Subscription`: async iterator over live events, backed by a bounded `asyncio.Queue(1000)`; a subscriber whose queue is full is dropped and its iterator ends after yielding one final synthetic `error` event `{"message": "subscriber lagged"}` (not persisted). `unsubscribe()`.
  - `append_threadsafe(type, data)` for callers outside the loop thread (uses `loop.call_soon_threadsafe`).
  - `close()`: ends every subscription.

## Acceptance conditions

- AC1: append assigns strictly increasing seq and persists JSONL [evidence: test:tests/test_event_log.py::test_append_assigns_increasing_seq]
- AC2: seq continues after reopening the file [evidence: test:tests/test_event_log.py::test_seq_continues_after_reopen]
- AC3: page honours after_seq, limit and has_more [evidence: test:tests/test_event_log.py::test_page_after_seq_and_limit]
- AC4: page reads from the file beyond the ring [evidence: test:tests/test_event_log.py::test_page_falls_back_to_file_beyond_ring]
- AC5: Rotation keeps the newest half of the lines [evidence: test:tests/test_event_log.py::test_rotation_keeps_newest_half]
- AC6: Subscribers receive live events in order [evidence: test:tests/test_event_log.py::test_subscriber_receives_live_events]
- AC7: A lagging subscriber is dropped with a final error event [evidence: test:tests/test_event_log.py::test_lagging_subscriber_dropped]
- AC8: A write failure does not raise and is logged once [evidence: test:tests/test_event_log.py::test_write_failure_does_not_raise]
- AC9: Unknown event types are rejected [evidence: test:tests/test_event_log.py::test_unknown_event_type_rejected]
- AC10: close ends subscriptions [evidence: test:tests/test_event_log.py::test_close_ends_subscriptions]

## Files

- `src/claude_code_wrapper/events.py`
- `tests/test_event_log.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
