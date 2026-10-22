---
title: Peer inbox outcomes
status: in-progress
repo: .
value: "A sender always hears when its message was rejected or dropped, and notices never hold up a turn's end"
files: [src/claude_code_wrapper/peer_inbox.py, tests/test_peer_inbox_outcomes.py]
verify: "uv run pytest tests/test_peer_inbox_outcomes.py tests/test_peer_inbox.py tests/test_stop_gate.py -q && mise run lint"
depends_on: [peer-inbox-delivery]
owns: [peer-inbox-outcome]
uses: [peer-inbox]
model: opus
estimate: 60
actual: ""
review: full
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/cross-container-peer-messagi--peer-inbox-outcomes
branch: cross-container-peer-messagi--peer-inbox-outcomes
verified_head: ""
run: ""
provides: []
---

# Peer inbox outcomes

## Requirement

Delivery outcomes of the peer inbox: the hook the peer service uses to send notices, run off the Stop hook path (design D8).

- src/claude_code_wrapper/peer_inbox.py:
  - `PeerInbox(..., on_outcome: Callable[[InboxItem, str, TurnRecord | None], Awaitable[None]] | None = None)`; `status` is `delivered`, `rejected` or `dropped`.
  - Rules: only items of kind `message` produce outcomes (notices never produce notices). `rejected` (onTurn veto) and `dropped` (`close()` with the item still queued) always produce an outcome; `delivered` produces one only when the item had `notify_when_idle`, once its handling turn reaches a terminal state (`turn_finished(record)`, with `record.state`).
  - Every `on_outcome` call runs as a background task (the router's `_spawn` or an inbox-owned task set), never awaited by `turn_finished`, the delivery attempt or `close()`; an exception in it is logged (redacted), never raised.
  - `async drain(timeout: float) -> None` waits for outstanding outcome tasks up to `timeout`, then cancels what is left; the manager calls it on shutdown before closing the client.
- tests/test_peer_inbox_outcomes.py (router harness, fake claude where a real session is needed): a notify_when_idle message produces exactly one `delivered` outcome with the turn's state after the turn completes, and a plain message none; an interrupted handling turn gives `delivered` with state `interrupted`; a vetoed message produces `rejected` with the veto reason; close with queued messages produces one `dropped` each; notice items never produce outcomes; an `on_outcome` that blocks for 10 s (an unreachable sender) does not delay the Stop relay answer: `router.handle_claude_hook("Stop", ...)` returns within 1 s and the turn completes; `drain(0.2)` returns within about 0.2 s and cancels the blocked task; an exception in `on_outcome` is logged and the inbox keeps delivering.

Interfaces: `PeerInbox(on_outcome=...)`, `PeerInbox.drain` (the `peer-inbox-outcome` part).

Notes: the router change of peer-inbox-delivery already calls `turn_finished` without awaiting; this task must keep it that way and does not touch router.py.

## Acceptance conditions

- AC1: a notify_when_idle message yields one delivered outcome with the handling turn state and a plain message none [evidence: test:tests/test_peer_inbox_outcomes.py::test_delivered_outcome_only_with_notify]
- AC2: a vetoed message always yields a rejected outcome with the reason [evidence: test:tests/test_peer_inbox_outcomes.py::test_rejected_always_notifies]
- AC3: closing with queued messages yields one dropped outcome each, and notices never yield outcomes [evidence: test:tests/test_peer_inbox_outcomes.py::test_dropped_always_notifies]
- AC4: a blocking outcome callback does not delay the Stop relay answer [evidence: test:tests/test_peer_inbox_outcomes.py::test_slow_sender_does_not_delay_stop]
- AC5: drain waits up to its timeout and cancels what is left [evidence: test:tests/test_peer_inbox_outcomes.py::test_drain_bounded]
- AC6: an exception in the outcome callback is logged and delivery goes on [evidence: test:tests/test_peer_inbox_outcomes.py::test_outcome_error_isolated]

## Files

- `src/claude_code_wrapper/peer_inbox.py`
- `tests/test_peer_inbox_outcomes.py`


## Interfaces

See the Requirement's Interfaces paragraph; keep every name and signature stated there.

## Expected outcome

Every message's outcome that the sender must hear about is reported through a background callback that never holds up the Stop hook.

## Steps

- Write the tests named in the acceptance conditions in `tests/test_peer_inbox_outcomes.py` first; run `uv run pytest tests/test_peer_inbox_outcomes.py -q` and see them fail for the missing behaviour (not an import typo).
- Implement the smallest change that makes them pass; run `uv run pytest tests/test_peer_inbox_outcomes.py -q` again and expect all passed.
- Run the task verify: `uv run pytest tests/test_peer_inbox_outcomes.py tests/test_peer_inbox.py tests/test_stop_gate.py -q && mise run lint`; expect all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

See the Requirement's Notes paragraph.
