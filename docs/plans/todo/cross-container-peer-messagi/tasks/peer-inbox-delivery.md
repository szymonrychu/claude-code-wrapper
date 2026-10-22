---
title: Peer inbox delivery
status: todo
repo: .
value: Peer messages reach an agent as orderly new turns and never disturb a running turn
files: [src/claude_code_wrapper/peer_inbox.py, src/claude_code_wrapper/router.py, src/claude_code_wrapper/turns.py, tests/test_peer_inbox.py, README.md]
verify: "uv run pytest tests/test_peer_inbox.py tests/test_router.py tests/test_session_turns.py tests/test_turns.py tests/test_command_runner.py -q && mise run lint"
depends_on: [peer-events-and-metrics, peer-http-client, peers-config-section]
owns: [peer-inbox]
uses: [peer-events, peer-metrics, peer-wire, peers-config]
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

# Peer inbox delivery

## Requirement

Per-session peer inbox that delivers messages and idle notices as new turns (design D7, D8 receiving half, D9 emission on the target).

- src/claude_code_wrapper/turns.py: `Origin` gains `"peer"`.
- New src/claude_code_wrapper/peer_inbox.py:
  - `@dataclass InboxItem {kind: "message" | "notice", from_address, msg_id, text, notify_when_idle: bool = False, state: str | None = None}`.
  - `frame_message(item) -> str` and `frame_notice(item) -> str` produce exactly the D7 and D8 frames; the message body has `&`, `<`, `>` escaped as `&amp;`, `&lt;`, `&gt;` and attribute values are escaped too, so a body containing `</agent-message>` cannot close the frame.
  - `class PeerInbox(router, *, max_items: int, metrics: Metrics | None = None, on_handled: Callable[[InboxItem, TurnRecord], Awaitable[None]] | None = None)`:
    - `put(item) -> int` appends and returns the queue depth; raises `PeerError("inbox_full", ...)` (from peer_client) when `max_items` items are already queued; a `(from_address, msg_id)` among the last 256 accepted is a duplicate: not queued, returns the current depth and reports `duplicate=True` through `put`'s caller-visible result (builder picks the shape: a tuple or a small result object). It emits `peer_message_received` `{msg_id, from, kind, bytes, message, queued}` on the router and counts `peer_received(kind, "accepted" | "duplicate" | "inbox_full")`. Then it calls `kick()`.
    - `kick()` schedules one delivery attempt on the next loop pass (a no-op when one is already scheduled or running, or after `close()`).
    - Delivery takes the head only when `router.session` is `ready` with no current turn, `router.runner_active` is false and no command is queued without a turn (`router.commands.pending()` entries with `turn_id is None`, which the idle runner owns). It calls `router.submit_turn(frame, origin="peer", metadata={"peer": {"from": ..., "msg_id": ..., "kind": ...}})`. On success it pops the head, emits `peer_message_delivered` `{msg_id, from, kind, turn_id, result: "delivered"}`, counts `peer_delivered(kind, "delivered")`, and remembers `turn_id -> item` when the item is a message with `notify_when_idle`. `TurnRejected` leaves the head queued. `TurnVetoed` pops it with result `rejected` (no notice later). Only one delivery attempt runs at a time; items go out strictly in arrival order, one turn per item.
    - `turn_finished(record)`: when `record.turn_id` was remembered, forgets it and awaits `on_handled(item, record)`; errors from `on_handled` are logged, never raised.
    - `close()`: drops every queued item with `peer_message_delivered` result `dropped` and stops scheduling.
- src/claude_code_wrapper/router.py:
  - `SessionRouter.peer_inbox: PeerInbox | None = None` (set by the manager later) and a public `emit_event(type, data) -> LogEvent` that redacts like `_emit`, for the inbox and the peer service.
  - `on_state`: on `ready` with an inbox, schedule `peer_inbox.kick()` on the next loop pass (after the idle command runner gets its chance, as `_start_idle_runner` is scheduled today).
  - `_after_turn`: with an inbox, `await peer_inbox.turn_finished(record)` before the callback delivery, so completed and failed turns both reach it.
  - `close()`: `peer_inbox.close()` before `session_closed`.
- README.md: the `peer` origin in the turn origin lists (`## Overview` origin sentence, `turn_started` data, `onTurn` text).
- tests/test_peer_inbox.py, with the router harness from tests/test_router.py and the fake claude where a real session is needed: frames and escaping; an idle session gets a message as a turn with origin peer and metadata; a busy session gets it after the running turn completes, never during it; three messages go out in arrival order as three turns; `inbox_max` overflow raises inbox_full and counts it; a duplicate msg_id is not queued twice; an onTurn deny drops the item with result rejected; a queued idle command runs before a waiting peer message; `turn_finished` calls `on_handled` once for a notify message and never for a plain one; `close()` drops queued items with result dropped; the received and delivered events carry the D9 keys.

Interfaces: `peer_inbox.PeerInbox`, `InboxItem`, `frame_message`, `frame_notice` (the `peer-inbox` part); `SessionRouter.peer_inbox`, `SessionRouter.emit_event`; `turns.Origin` value `peer`.

Notes: this is the concurrency-heavy task. `submit_turn` awaits the onTurn hook, so the session can change state between the readiness check and the paste; rely on `TurnRejected` from `check_submittable`/`submit`, not on the earlier check. Never write to the PTY directly.

## Acceptance conditions

- AC1: message and notice frames match design D7 and D8 and escape the body so it cannot close the frame [evidence: test:tests/test_peer_inbox.py::test_frames_and_escaping]
- AC2: a message to an idle session starts a turn with origin peer and peer metadata and emits received and delivered events [evidence: test:tests/test_peer_inbox.py::test_idle_session_gets_turn]
- AC3: a message to a busy session is delivered as a new turn only after the running turn completes [evidence: test:tests/test_peer_inbox.py::test_busy_session_waits_for_turn_end]
- AC4: several messages are delivered in arrival order, one turn each [evidence: test:tests/test_peer_inbox.py::test_arrival_order]
- AC5: a full inbox raises inbox_full and a repeated msg_id from the same sender is not queued twice [evidence: test:tests/test_peer_inbox.py::test_bounds_and_duplicates]
- AC6: an onTurn deny drops the item with result rejected and no handled callback [evidence: test:tests/test_peer_inbox.py::test_veto_drops_item]
- AC7: a queued idle command runs before a waiting peer message [evidence: test:tests/test_peer_inbox.py::test_commands_go_first]
- AC8: turn_finished calls on_handled once for a notify_when_idle message and never for a plain one [evidence: test:tests/test_peer_inbox.py::test_handled_callback]
- AC9: close drops queued items with result dropped [evidence: test:tests/test_peer_inbox.py::test_close_drops]
- AC10: the existing router, turn and command suites stay green [evidence: command:uv run pytest tests/test_router.py tests/test_session_turns.py tests/test_turns.py tests/test_command_runner.py tests/test_submit_busy_first.py tests/test_readme_api.py -q]

## Files

- `src/claude_code_wrapper/peer_inbox.py`
- `src/claude_code_wrapper/router.py`
- `src/claude_code_wrapper/turns.py`
- `tests/test_peer_inbox.py`
- `README.md`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
