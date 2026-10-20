---
title: Session turns
status: in-progress
repo: claude-code-wrapper
value: "Turns submit, complete, interrupt and time out without ever wedging the slot"
files: [src/claude_code_wrapper/session.py, src/claude_code_wrapper/turns.py, tests/test_session_turns.py, tests/test_turns.py]
verify: "uv run pytest tests/test_session_turns.py tests/test_turns.py tests/test_session_boot.py -q && mise run lint"
depends_on: [session-boot]
owns: [turn-store]
uses: [session, fake-claude]
model: opus
estimate: 90
actual: ""
review: full
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Session turns

## Requirement

Add turn control to `Session` in `src/claude_code_wrapper/session.py` and build the turn store in `src/claude_code_wrapper/turns.py` (D13, D11).

turns.py:
- `new_turn_id()` -> `turn-<base36 nanoseconds>`, strictly increasing within the process even when the clock returns the same or a smaller value.
- `TurnRecord` (pydantic) with the design.md fields: `turn_id, session_id, origin (client|initial|agent), state (running|completed|failed|interrupted|timeout), text, final_text, stop_reason, usage, model_tokens, error, callback_url, started_at, completed_at, last_activity_at, stall_suspected_since, tool_calls`; `summary()` (turn_id, origin, state, started_at, completed_at, stop_reason).
- `TurnStore(path)`: `put(record)` appends the full record as a JSON line on every change; on open it loads the file keeping the last line per turn_id (corrupt lines skipped); `get(id)`, `list()` (in start order), `current` (the running one, if any).

Session additions:
- `async submit(text, *, origin="client", callback_url=None) -> TurnRecord`: state must be `ready` (booting -> `TurnRejected("booting")`, dead or stopped -> `TurnRejected("dead")`, busy -> `TurnRejected("busy")`); the record is created and the slot reserved BEFORE any write; when `sessions.turn_marker` the pasted text is `text + "\n\n<!-- ccw turn: <turn_id> -->"`; via `pty.send_prompt(prompt, submit_delay_seconds)` (paste, sleep, CR as two separate writes under one hold of the write lock); state busy; arms the stall timer and the optional hard limit.
- `note_activity()`: resets the stall timer and updates `last_activity_at`.
- Stall: no activity for `turn_stall_seconds` sets `stall_suspected_since` and calls `listener.on_stall(turn_id)` once; it never fails the turn.
- Hard limit: `limits.turn_max_seconds > 0` expiring -> interrupt, then the turn ends with state `timeout`.
- `complete(payload: dict, *, usage=None, model_tokens=None, marker_turn_id=None) -> TurnRecord | None` (from the Stop relay): accepted when `payload["session_id"] == claude_session_id` and state is busy and (`marker_turn_id` is None or equals the current turn); sets `final_text = payload["last_assistant_message"]`, `stop_reason = "end_turn"`, usage fields, state ready, persists, returns it. When state is ready and no turn is running, a matching Stop creates and returns a completed record with `origin="agent"` (background Workflow/Monitor completion). Otherwise (session id mismatch, booting, dead, marker of another turn) returns None and changes nothing.
- `async interrupt() -> str | None`: idle -> None. Busy -> writes a single ESC, then waits for `on_interrupted()` (called by the router when the transcript interrupt line appears) up to `interrupt_settle_seconds`; on timeout writes a second ESC. Either way the turn ends with state `interrupted` and the slot is freed; returns the turn id.
- `on_interrupted()`: resolves a pending interrupt (or, unprompted, ends the running turn as interrupted).
- `async probe(text=None) -> Probe`: writes paste + delay + CR without taking the slot (busy or ready); default text `Reply with CCW-ALIVE and one sentence on what you are doing, then continue.`; text gets `<!-- ccw probe: <probe_id> -->`; probe states `pending -> delivered` (router calls `probe_delivered(id)` on the transcript marker) `-> answered` (router calls `probe_answered()` when assistant text containing `CCW-ALIVE` follows); `get_probe(id)`.
- Fatal pattern while busy -> the turn fails with `error = <pattern>`, state ready. claude exiting while busy -> the turn fails with `error = "claude_exited"` (no automatic re-paste).
- Every terminal transition persists the record and calls `listener.on_turn_end(record)`.

Settled by the builder check (binding):
- The store comes from the constructor kwarg `turn_store=`; `TurnStore(path, readonly=False)`: with `readonly=True` it loads and serves `get`/`list` but `put` raises (used for stopped sessions).
- `Session.record_tool_call()` increments the running turn's `tool_calls` and persists it (no-op when idle).
- `probe` also writes through `pty.send_prompt`.

Settled at the gate (D19-D24, binding):
- `TurnRecord.callback_url` serialises as `callbackUrl` (pydantic alias, `populate_by_name`) in every API-facing dump and in turns.jsonl.

## Acceptance conditions

- AC1: submit writes the paste and a separate CR with the turn marker [evidence: test:tests/test_session_turns.py::test_submit_writes_paste_then_cr_and_marker]
- AC2: submit while busy is rejected as busy [evidence: test:tests/test_session_turns.py::test_submit_busy_rejected]
- AC3: submit while booting or dead is rejected with that reason [evidence: test:tests/test_session_turns.py::test_submit_booting_and_dead_rejected]
- AC4: A Stop payload completes the turn with last_assistant_message [evidence: test:tests/test_session_turns.py::test_complete_from_stop_payload]
- AC5: A Stop with another session id or another turn marker is ignored [evidence: test:tests/test_session_turns.py::test_stale_stop_ignored]
- AC6: An unsolicited Stop while idle creates an agent-origin turn [evidence: test:tests/test_session_turns.py::test_unsolicited_stop_creates_agent_turn]
- AC7: Interrupt ends the turn as interrupted when the transcript line arrives [evidence: test:tests/test_session_turns.py::test_interrupt_fails_turn_interrupted]
- AC8: Interrupt escalates with a second ESC after the settle time and frees the slot [evidence: test:tests/test_session_turns.py::test_interrupt_escalates_after_settle]
- AC9: A fatal pattern fails the running turn and frees the slot [evidence: test:tests/test_session_turns.py::test_fatal_fails_turn]
- AC10: A stall is reported but does not fail the turn [evidence: test:tests/test_session_turns.py::test_stall_reported_not_failed]
- AC11: turn_max_seconds ends the turn as timeout [evidence: test:tests/test_session_turns.py::test_turn_max_seconds_times_out]
- AC12: A probe is written without taking the slot and becomes delivered [evidence: test:tests/test_session_turns.py::test_probe_delivered]
- AC13: claude exiting mid-turn fails the turn with claude_exited [evidence: test:tests/test_session_turns.py::test_exit_during_turn_fails_turn]
- AC14: Turn ids are strictly increasing under a repeating clock [evidence: test:tests/test_turns.py::test_turn_ids_monotonic_unique]
- AC15: The turn store persists and reloads the last state per turn [evidence: test:tests/test_turns.py::test_store_persists_and_reloads_last_state]
- AC16: The turn store lists summaries in start order [evidence: test:tests/test_turns.py::test_list_summaries]

- AC17: record_tool_call counts tool calls on the running turn [evidence: test:tests/test_session_turns.py::test_record_tool_call_counts]
- AC18: A read-only turn store serves records and rejects writes [evidence: test:tests/test_turns.py::test_readonly_store]

- AC19: TurnRecord serialises callbackUrl in camelCase [evidence: test:tests/test_turns.py::test_turn_record_serializes_callback_url_camel_case]

## Files

- `src/claude_code_wrapper/session.py`
- `src/claude_code_wrapper/turns.py`
- `tests/test_session_turns.py`
- `tests/test_turns.py`

## Interfaces

- Part `turn-store`: `claude_code_wrapper.turns` (`TurnRecord`, `TurnStore`, `new_turn_id`).
- `Session.submit`, `complete`, `interrupt`, `on_interrupted`, `probe`, `probe_delivered`, `probe_answered`, `get_probe`, `note_activity`; listener additions `on_stall`, `on_turn_end`; `TurnRejected(reason)`.

## Expected outcome

Every way a turn can end (Stop hook, interrupt with or without the transcript line, fatal screen text, claude exit, hard limit) frees the slot exactly once and leaves a persisted record; a late Stop can never end the wrong turn.

## Steps

- Write `tests/test_turns.py`; red; implement turns.py; green.
- Write `tests/test_session_turns.py` with the fake claude; its settings.json Stop hook is `cat >> <tmp>/hooks.jsonl`, and the test feeds those payloads to `session.complete` (the real relay path is covered by internal-api and e2e); red; implement; green; `mise run lint`.

## Notes

- Sources: tatara `session.go:1154-1235` (slot reserved before write, two writes), `:1538-1646` (stale rejection), `interrupt.go:55`, `probe.go:15`; magic `M/session.py:505-547, 591-684, 728-809`, `M/turns.py:21-64`.
- Pitfalls: claude runs no Stop hook for an interrupted turn (magic incident HO1: 5523 consecutive 409s); Stop payloads carry no turn id (the marker exists for that); silence is not a stall.
