---
title: Peer service core
status: todo
repo: .
value: "One object per ccw instance runs membership, signed sending, receiving and notices for every session"
files: [src/claude_code_wrapper/peer_service.py, tests/test_peer_service.py]
verify: "uv run pytest tests/test_peer_service.py -q && mise run lint"
depends_on: [peer-inbox-outcomes, peer-messages-endpoint, peers-mcp-tools, peer-membership-service, peer-http-client, peer-events-and-metrics]
owns: [peer-service]
uses: [peers-config, peer-member-table, peer-wire, peer-inbox, peer-inbox-outcome, peer-messages-endpoint, ccw-mcp-peers-tool, peer-membership, peer-events, peer-metrics]
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

# Peer service core

## Requirement

The instance-wide peer service: membership plus messaging for every session (design D1, D4 send side, D7 registry, D8 notices, D9 emission). New file src/claude_code_wrapper/peer_service.py; the manager wiring is the next task.

- `class PeerService(cfg: PeersConfig, *, public_port: int, redactor: Redactor, dispatcher: HookDispatcher, metrics: Metrics | None = None, transport: httpx.AsyncBaseTransport | None = None)`:
  - `instance` (`resolve_instance_name`), `url` (`resolve_advertise_url`), a `PeerClient(cfg.token)`, a `MemberTable` seeded with `cfg.static` (seeds) and a `MembershipService` whose `own_sessions` are this instance's registered sessions and whose `on_change` emits `peer_member_joined` / `peer_member_left` `{instance, url, reason}` on every registered session's router. The token is added to the redactor at construction.
  - `async join()` / `async leave()` delegate to the membership service. `async stop(drain_timeout)`: drains every inbox's outcome tasks (`PeerInbox.drain`) within `drain_timeout`, then closes the client. The manager calls `leave()` before any onDestroy and `stop()` after the sessions are down.
  - `register(session_id, router, *, name, cwd) -> PeerInbox`: builds `PeerInbox(router, max_items=cfg.inbox_max, max_per_minute=cfg.max_messages_per_minute, metrics=..., on_outcome=self._notify)`, sets `router.peer_inbox`, adds the session to the local list and calls `membership.sessions_changed()`. `unregister(session_id)`: removes it and calls `sessions_changed()` (the router closes the inbox; its dropped items' outcomes still reach `_notify`).
  - `PeerTools` (mcp_server): `async list_peers(caller_session, instance)` -> our other sessions plus `table.sessions(...)` of reachable members, excluding the caller, sorted, filtered; `async send(caller_session, to, message, notify_when_idle) -> msg_id`: validates the address and not-self (`invalid`), the UTF-8 size (`too_large`), resolves `to` to our own url (a local session) or `table.lookup(to)` (`unknown_peer` when neither), sends a `MessageBody` (its `PeerError` propagates), emits `peer_message_sent` on the caller's router and counts `peer_sent("message", result)` for success and every failure.
  - `PeerReceiver` (api.peers): `accept_message(body)` -> `PeerError("not_found")` unless `body.to_session` is registered here, else `inbox.put(InboxItem(kind="message", ...))` and `{msg_id, queued}` (plus `duplicate`); `accept_notice(body)` the same with `kind="notice"`, status, state and reason; `accept_heartbeat` (including a `leaving: true` view) delegates to the membership service; there is no accept_join or accept_leave (approval O).
  - `_notify(item, status, record)`: resolves the sender address like `send` does; if found sends a `NoticeBody {from: <the receiving session's address>, to_session, msg_id, status, state, reason}`, emits `peer_message_sent` with `kind: "notice"` and counts it; an unknown sender or a `PeerError` is logged (redacted) and counted, never raised or re-queued.
- tests/test_peer_service.py, two PeerService objects with an `httpx.MockTransport` routing each url to the other's `create_app` (signed requests end to end), stub routers from the test_router harness: after both join (B seeded with A), each lists the other's sessions; register on A shows up in B's listing after the immediate heartbeat; a session-local send on one instance works; a cross-instance send reaches the target inbox and emits the sent event; each failure (bad address, self, too large, unknown peer, target inbox full, target rate limited, unreachable) maps to its PeerError and a counted result; accept_message for an unknown session is not_found; delivered-with-notify, rejected and dropped messages each produce exactly one notice that lands in the sender's inbox; member joined and left events appear on registered sessions; `stop()` drains a pending notice within its timeout; the token never appears in logs, events or captured requests.

Interfaces: `peer_service.PeerService` (implements `mcp_server.PeerTools` and `api.peers.PeerReceiver`; the `peer-service` part).

## Acceptance conditions

- AC1: after joining, each service lists the other's sessions and a newly registered session reaches the other after the immediate heartbeat [evidence: test:tests/test_peer_service.py::test_membership_listing]
- AC2: a signed send reaches the target inbox locally and across instances and emits peer_message_sent with result ok [evidence: test:tests/test_peer_service.py::test_send_reaches_target_inbox]
- AC3: bad address, self address, too large, unknown peer, inbox full, rate limited and unreachable each raise their PeerError and are counted [evidence: test:tests/test_peer_service.py::test_send_failures]
- AC4: accept_message for a session not registered here raises not_found [evidence: test:tests/test_peer_service.py::test_accept_unknown_session]
- AC5: delivered with notify_when_idle, rejected and dropped messages each send exactly one notice that lands in the sender inbox [evidence: test:tests/test_peer_service.py::test_notices_round_trip]
- AC6: member joined and left events appear on every registered session [evidence: test:tests/test_peer_service.py::test_member_events]
- AC7: stop drains pending notices within its timeout and closes the client [evidence: test:tests/test_peer_service.py::test_stop_drains]
- AC8: the peer token never appears in logs, events or captured requests [evidence: test:tests/test_peer_service.py::test_token_never_exposed]

## Files

- `src/claude_code_wrapper/peer_service.py`
- `tests/test_peer_service.py`


## Interfaces

See the Requirement's Interfaces paragraph; keep every name and signature stated there.

## Expected outcome

One PeerService per instance publishes sessions, lists and sends for the MCP tools, receives for the HTTP routes and sends every required notice.

## Steps

- Write the tests named in the acceptance conditions in `tests/test_peer_service.py` first; run `uv run pytest tests/test_peer_service.py -q` and see them fail for the missing behaviour (not an import typo).
- Implement the smallest change that makes them pass; run `uv run pytest tests/test_peer_service.py -q` again and expect all passed.
- Run the task verify: `uv run pytest tests/test_peer_service.py -q && mise run lint`; expect all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

Review is full: shared state across sessions, background heartbeat and notice tasks, and a secret token. Keep the token out of every log line, event and request.
