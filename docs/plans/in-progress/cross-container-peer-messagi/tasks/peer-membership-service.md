---
title: Peer membership service
status: done
repo: .
value: "An instance joins the mesh through its seeds, stays current by heartbeat and leaves cleanly, with hooks in order"
files: [src/claude_code_wrapper/peer_membership.py, tests/test_peer_membership_service.py]
verify: "uv run pytest tests/test_peer_membership_service.py -q && mise run lint"
depends_on: [peer-member-table, peer-http-client, peer-membership-hooks, peer-messages-endpoint, peer-events-and-metrics]
owns: [peer-membership]
uses: [peer-member-table, peer-wire, peer-membership-hooks, peer-messages-endpoint, peer-metrics, peers-config]
model: opus
estimate: 90
actual: ""
review: full
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/cross-container-peer-messagi--peer-membership-service
branch: cross-container-peer-messagi--peer-membership-service
verified_head: d618f95455669bcf4856ec6758b1ca5d741f0946
run: cross-container-peer-messagi
provides: []
acs_for_audit: [AC4]
process_gaps: []
---

# Peer membership service

## Requirement

The membership runtime: join, heartbeat and leave with the four hooks, over the heartbeat route only (approval O; design D5, D9 membership events and metrics, D15). New file src/claude_code_wrapper/peer_membership.py.

- `class MembershipService(cfg: PeersConfig, *, instance, url, client: PeerClient, table: MemberTable, dispatcher: HookDispatcher, own_sessions: Callable[[], list[PeerSession]], on_change: Callable[[str, Member, str], None] | None = None, metrics: Metrics | None = None)`:
  - `async join() -> dict`: `fire_instance("preJoin", {instance, url, members})`; a deny sets `standalone = True`, counts `peer_membership("join", "vetoed")`, skips the calls and the heartbeat loop. Otherwise the join is the first heartbeat round: it POSTs a heartbeat with `table.view(own_sessions())` to every `table.targets()` concurrently, each under `join_timeout_seconds`; each answer goes to `table.record_report`, each failure into `failures` (`{url, error}`) and `peer_membership("join", "ok" | "failed")`; then `fire_instance("postJoin", {instance, url, members, failures, standalone})` and starts the heartbeat loop. Returns the postJoin data. Total time is bounded by the hook timeouts plus `join_timeout_seconds`, whatever the seeds do.
  - Heartbeat loop: every `heartbeat_seconds` one round: POSTs heartbeat with the current view to every target concurrently (timeout `heartbeat_seconds`), `record_report` on success, `record_failure` on failure, counts `peer_membership("heartbeat", ...)`, updates `peer_members(len(reachable))`. A round never overlaps the next; errors are logged and the loop goes on.
  - `sessions_changed()`: schedules one immediate round (debounced: at most one extra round queued while one runs). Never awaited by callers.
  - Receiver side (used by `PeerService`, the `api.peers.PeerReceiver` membership methods): `accept_heartbeat(view)` records the report and returns our view (a standalone instance still answers); a view with `leaving: true` removes the member at once (reason `leave`) instead of recording it. `TableFull` becomes `PeerError("rate_limited")`.
  - `on_change(kind, member, reason)` fires with `kind` `joined` or `left` and reason `join`, `heartbeat`, `leave` or `missed_heartbeats` whenever a member enters or leaves the reachable list (the peer service turns it into `peer_member_joined` / `peer_member_left` events on every live session).
  - `async leave() -> dict`: stops the heartbeat loop first, `fire_instance("preLeave", ...)` (decision ignored), POSTs one final heartbeat with `leaving: true` and no sessions to every reachable member concurrently under `join_timeout_seconds`, `fire_instance("postLeave", {instance, url, members, failures})`, returns that data. Idempotent: a second call returns at once. Skipped calls (but still both hooks) when standalone.
  - `standalone`, `table` properties.
- tests/test_peer_membership_service.py, with an `httpx.MockTransport` routing urls to stub peers (or to another MembershipService's receiver methods) and a recording dispatcher built by `hooks.instance_dispatcher` with command actions writing marker files: join order preJoin -> calls -> postJoin with members and failures; a preJoin deny makes the instance standalone with no calls and no heartbeats; an unreachable or hanging seed makes join return within `join_timeout_seconds` plus slack and the failure is listed, then a later heartbeat round reaches it once it answers; three services where C knows only B and B knows A converge so every one lists the other two within a few rounds; a member that stops answering is dropped after `max_missed_heartbeats` rounds with reason missed_heartbeats and re-added with reason heartbeat when it answers again; a `leaving: true` heartbeat drops it at once with reason leave; `sessions_changed()` triggers a round without waiting for the interval and coalesces bursts; leave order preLeave -> calls -> postLeave with the loop stopped first, bounded by the timeout against a hanging member; no call ever waits on the event loop beyond its timeout (a concurrent ticker keeps ticking).

Interfaces: `peer_membership.MembershipService` (the `peer-membership` part).

Notes: heartbeats carry only `{instance, url, sessions, members, leaving}`; there is no join or leave call; sessions are trusted only from a member's own report (peer-member-table rules).

## Acceptance conditions

- AC1: join runs preJoin, heartbeats every target and runs postJoin with members and failures, in that order [evidence: test:tests/test_peer_membership_service.py::test_join_order_and_hooks]
- AC2: a preJoin deny leaves the instance standalone with no heartbeat calls [evidence: test:tests/test_peer_membership_service.py::test_prejoin_veto_standalone]
- AC3: an unreachable or hanging seed does not hold join past join_timeout_seconds and is reached by a later heartbeat [evidence: test:tests/test_peer_membership_service.py::test_unreachable_seed_bounded_then_retried]
- AC4: three instances seeded in a chain converge so each lists the other two [evidence: test:tests/test_peer_membership_service.py::test_gossip_converges]
- AC5: a silent member is dropped after max_missed_heartbeats and re-added when it answers again, and a leaving heartbeat drops it at once [evidence: test:tests/test_peer_membership_service.py::test_missed_heartbeats_leave_and_readd]
- AC6: sessions_changed triggers an immediate debounced heartbeat round [evidence: test:tests/test_peer_membership_service.py::test_sessions_changed_pushes]
- AC7: leave stops heartbeats, runs preLeave, sends a leaving heartbeat to every member and runs postLeave, bounded against a hanging member [evidence: test:tests/test_peer_membership_service.py::test_leave_order_and_bound]
- AC8: membership I/O never blocks the event loop beyond its timeouts [evidence: test:tests/test_peer_membership_service.py::test_loop_never_blocked]

## Files

- `src/claude_code_wrapper/peer_membership.py`
- `tests/test_peer_membership_service.py`


## Interfaces

See the Requirement's Interfaces paragraph; keep every name and signature stated there.

## Expected outcome

An instance joins through its seeds, converges with the mesh by heartbeat, tracks reachability and leaves cleanly, with the four hooks in order and every wait bounded.

## Steps

- Write the tests named in the acceptance conditions in `tests/test_peer_membership_service.py` first; run `uv run pytest tests/test_peer_membership_service.py -q` and see them fail for the missing behaviour (not an import typo).
- Implement the smallest change that makes them pass; run `uv run pytest tests/test_peer_membership_service.py -q` again and expect all passed.
- Run the task verify: `uv run pytest tests/test_peer_membership_service.py -q && mise run lint`; expect all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

See the Requirement's Notes paragraph. Review is full: background loops, timeouts and ordering.
- Builder check: once `leave()` has run, `accept_heartbeat` raises `PeerError` for a non-leaving view (the endpoint answers 409 `left`) so peers cannot re-add us during shutdown; add a test.
