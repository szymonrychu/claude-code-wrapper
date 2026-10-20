---
title: Event log
status: done
repo: claude-code-wrapper
value: "Durable, pageable, streamable session history"
files: [src/claude_code_wrapper/events.py, tests/test_event_log.py]
verify: "uv run pytest tests/test_event_log.py -q && mise run lint"
depends_on: [repo-scaffold]
owns: [event-log]
uses: []
model: ""
estimate: 60
actual: 1
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/claude-code-wrapper-microser--event-log
branch: claude-code-wrapper-microser--event-log
verified_head: 1e097790658b366b38042a4b94fb9251020035ab
run: claude-code-wrapper-microser
provides: []
acs_for_audit: []
process_gaps: []
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

Settled by the builder check (binding):
- `EventLog(..., readonly=False)`: with `readonly=True` it reads the file for `page`/`last` (and `subscribe` ends immediately) while `append` raises; used for stopped sessions.


- Rotation fix (run 2): `_rotate` must decode the existing file with the same tolerant decoding as `_read_file` (`errors="replace"`), so a crash-truncated mid-multibyte tail line never disables rotation (AC12).

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
- AC11: A read-only log pages from disk and rejects appends [evidence: test:tests/test_event_log.py::test_readonly_log_pages_and_rejects_append]
- AC12: Rotation survives a corrupt (mid-multibyte) tail line: _rotate reads with the same tolerant decoding as _read_file (errors=replace), so after a crash-truncated tail the file still rotates once it passes max_bytes and never grows unbounded [evidence: test:tests/test_event_log.py::test_rotation_survives_corrupt_tail]

## Files

- `src/claude_code_wrapper/events.py`
- `tests/test_event_log.py`

## Interfaces

- Part `event-log`: `claude_code_wrapper.events.EventLog`, `Event`, `EVENT_TYPES`, `Subscription`. Consumers: event-router (writer), api-turns-events (history and SSE), readme-api (event reference).

## Expected outcome

Every session has a durable, pageable, streamable event history whose seq numbers are safe to use as SSE ids across service restarts.

## Steps

- Write `tests/test_event_log.py`; red; implement; green; `mise run lint`.

## Notes

- Source: ros2 `R/events.py:54-226` (seq across restarts, ring, paging, half-drop rotation, subscriber fan-out).

- 2026-10-20 amended: run1 round-cap: reviewer medium finding (rotation disabled after corrupt tail) is real; fix in run2
