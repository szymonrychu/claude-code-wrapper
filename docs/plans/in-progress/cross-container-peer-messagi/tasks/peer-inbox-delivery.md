---
title: Peer inbox delivery
status: in-progress
repo: .
value: "Peer messages reach an agent as orderly new turns, never disturb a running turn and are never stranded"
files: [src/claude_code_wrapper/peer_inbox.py, src/claude_code_wrapper/router.py, src/claude_code_wrapper/turns.py, tests/test_peer_inbox.py]
verify: "uv run pytest tests/test_peer_inbox.py tests/test_router.py tests/test_session_turns.py tests/test_turns.py tests/test_command_runner.py tests/test_auto_compact.py -q && mise run lint"
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

Per-session peer inbox: queueing, bounds, rate limit and delivery as new turns (design D7, D9 emission on the target, D14 dedupe). Outcome notices are the next task (peer-inbox-outcomes).

- src/claude_code_wrapper/turns.py: `Origin` gains `"peer"`.
- New src/claude_code_wrapper/peer_inbox.py:
  - `@dataclass InboxItem {kind: "message" | "notice", from_address, msg_id, text, notify_when_idle: bool = False, status: str | None = None, state: str | None = None, reason: str | None = None}`.
  - `frame_message(item) -> str` produces exactly the D7 frame, ending `Reply with mcp__ccw__send_message to "<from>" only if a reply is needed.`; the body has `&`, `<`, `>` escaped and attribute values are escaped too, so a body containing `</agent-message>` cannot close the frame. `frame_notice(item) -> str` produces the D8 `<peer-idle-notice .../>` frame for status `delivered` and the `<peer-delivery-failed .../>` frame for `rejected` and `dropped`.
  - `@dataclass PutResult {depth: int, duplicate: bool}`.
  - `class PeerInbox(router, *, max_items: int, max_per_minute: int, metrics: Metrics | None = None, clock=time.monotonic)`:
    - `put(item) -> PutResult`: a `(from_address, msg_id, kind)` among the last 256 accepted is a duplicate (not queued, `duplicate=True`); a message (never a notice) beyond `max_per_minute` from that `from_address` in the last 60 s raises `PeerError("rate_limited", ...)`; a queue already holding `max_items` raises `PeerError("inbox_full", ...)`. Accepted items emit `peer_message_received` `{msg_id, from, kind, bytes, message, queued}` on the router; every outcome counts `peer_received(kind, "accepted" | "duplicate" | "rate_limited" | "inbox_full")`. Then `kick()`.
    - `kick()`: schedules one delivery attempt on the next loop pass; while an attempt is scheduled or running a further kick sets a pending flag instead of being lost; after `close()` it does nothing.
    - Delivery attempt: takes the head only when `router.session` is `ready` with no current turn, `router.runner_active` is false and no command is queued without a turn (the idle runner's). It calls `router.submit_turn(frame, origin="peer", metadata={"peer": {"from", "msg_id", "kind"}})`. Success pops the head, emits `peer_message_delivered` `{msg_id, from, kind, turn_id, result: "delivered"}` and counts `peer_delivered(kind, "delivered")`. `TurnRejected` leaves the head queued. `TurnVetoed` pops it with result `rejected` (reason kept on the item for the next task). Any other exception is logged and leaves the head queued. When the attempt ends, whatever happened, the inbox runs again on the next loop pass if the pending flag is set or items remain and the session is ready, so a remaining item is never stranded; items go out strictly in arrival order, one turn each, with at most one attempt in flight.
    - `delivered_turn(turn_id) -> InboxItem | None` and `close() -> list[InboxItem]` (returns the dropped queued items after emitting `peer_message_delivered` result `dropped` for each and counting it; the next task sends their notices) are the hooks the outcome task builds on; `close()` stops scheduling.
- src/claude_code_wrapper/router.py:
  - `SessionRouter.peer_inbox: PeerInbox | None = None` (set by the manager later) and a public `emit_event(type, data) -> LogEvent` that redacts like `_emit`.
  - `on_state`: on `ready` with an inbox, schedule `peer_inbox.kick()` on the next loop pass, after the idle command runner's own scheduling.
  - `_after_turn`: with an inbox, call the synchronous `peer_inbox.turn_finished(record)` (never awaited, so the Stop relay answer is not delayed); in this task it only forgets the turn mapping.
  - `close()`: `peer_inbox.close()` before `session_closed`.
- tests/test_peer_inbox.py (router harness from tests/test_router.py, the fake claude where a real session is needed): frames and escaping, including the "only if a reply is needed" line; an idle session gets a message as a turn with origin peer and metadata; a busy session gets it only after the running turn completes; three messages go out in arrival order as three turns; a queue of three whose head is vetoed delivers the other two; a `kick()` arriving while an attempt is in flight is honoured after it; an interrupted peer turn is followed by the next queued item; an idle auto-compact (`request_command(..., origin="idle")`) that takes the slot just before the pop leaves the head queued and it is delivered after the compact; `inbox_max` overflow raises inbox_full; the 31st message in a minute from one sender raises rate_limited while another sender and notices still pass; a duplicate is not queued twice; `close()` drops queued items with result dropped; the events carry the D9 keys.

Interfaces: `peer_inbox.PeerInbox`, `InboxItem`, `PutResult`, `frame_message`, `frame_notice` (the `peer-inbox` part); `SessionRouter.peer_inbox`, `SessionRouter.emit_event`; `turns.Origin` value `peer`.

Notes: the concurrency core of the epic. `submit_turn` awaits the onTurn hook, so the session can change state between the readiness check and the paste; rely on `TurnRejected` from `check_submittable`/`submit`, not on the earlier check. Never write to the PTY directly. The `peer` origin is documented in the README by peers-readme-guide; this task does not edit README.md.

## Acceptance conditions

- AC1: message and notice frames match design D7 and D8, say to reply only if needed and escape the body [evidence: test:tests/test_peer_inbox.py::test_frames_and_escaping]
- AC2: a message to an idle session starts a turn with origin peer and peer metadata and emits received and delivered events [evidence: test:tests/test_peer_inbox.py::test_idle_session_gets_turn]
- AC3: a message to a busy session is delivered as a new turn only after the running turn completes [evidence: test:tests/test_peer_inbox.py::test_busy_session_waits_for_turn_end]
- AC4: several messages are delivered in arrival order, one turn each [evidence: test:tests/test_peer_inbox.py::test_arrival_order]
- AC5: when the head of a three-item queue is vetoed the other two are still delivered [evidence: test:tests/test_peer_inbox.py::test_veto_of_head_does_not_strand_queue]
- AC6: a kick arriving during an attempt is honoured after it ends [evidence: test:tests/test_peer_inbox.py::test_kick_mid_attempt]
- AC7: after an interrupted peer turn the next queued item is delivered [evidence: test:tests/test_peer_inbox.py::test_interrupt_then_next]
- AC8: an idle auto-compact racing the pop keeps the head queued and it is delivered after the compact [evidence: test:tests/test_peer_inbox.py::test_idle_compact_races_pop]
- AC9: a full inbox raises inbox_full, a sender over max_per_minute raises rate_limited, and a repeated msg_id is not queued twice [evidence: test:tests/test_peer_inbox.py::test_bounds_rate_limit_and_duplicates]
- AC10: close drops queued items with result dropped and returns them [evidence: test:tests/test_peer_inbox.py::test_close_drops]
- AC11: the existing router, turn and command suites stay green [evidence: command:uv run pytest tests/test_router.py tests/test_session_turns.py tests/test_turns.py tests/test_command_runner.py tests/test_submit_busy_first.py tests/test_auto_compact.py -q]

## Files

- `src/claude_code_wrapper/peer_inbox.py`
- `src/claude_code_wrapper/router.py`
- `src/claude_code_wrapper/turns.py`
- `tests/test_peer_inbox.py`


## Interfaces

See the Requirement's Interfaces paragraph; keep every name and signature stated there.

## Expected outcome

Queued peer messages become new turns with origin peer, strictly in order, only when the session is idle, and no item is ever stranded.

## Steps

- Write the tests named in the acceptance conditions in `tests/test_peer_inbox.py` first; run `uv run pytest tests/test_peer_inbox.py -q` and see them fail for the missing behaviour (not an import typo).
- Implement the smallest change that makes them pass; run `uv run pytest tests/test_peer_inbox.py -q` again and expect all passed.
- Run the task verify: `uv run pytest tests/test_peer_inbox.py tests/test_router.py tests/test_session_turns.py tests/test_turns.py tests/test_command_runner.py tests/test_auto_compact.py -q && mise run lint`; expect all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

See the Requirement's Notes paragraph. Review is full: this is the concurrency core of the epic.
