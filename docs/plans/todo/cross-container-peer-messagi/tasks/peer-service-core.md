---
title: Peer service core
status: todo
repo: .
value: "One object per ccw instance runs discovery, signed sending, receiving and notices for every session"
files: [src/claude_code_wrapper/peer_service.py, tests/test_peer_service.py]
verify: "uv run pytest tests/test_peer_service.py -q && mise run lint"
depends_on: [peer-inbox-outcomes, peer-messages-endpoint, peers-mcp-tools, peer-directory-module, peer-http-client, peer-events-and-metrics]
owns: [peer-service]
uses: [peers-config, peer-directory-entry, peer-wire, peer-inbox, peer-inbox-outcome, peer-messages-endpoint, ccw-mcp-peers-tool, peer-events, peer-metrics]
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

The instance-wide peer service that ties directory, client and inboxes together (design D1, D4 send side, D5 runtime, D7 registry, D8 notices, D9 emission on the sender). New file src/claude_code_wrapper/peer_service.py; the manager wiring is the next task.

- `class PeerService(cfg: PeersConfig, *, public_port: int, redactor: Redactor, metrics: Metrics | None = None, transport: httpx.AsyncBaseTransport | None = None, clock=time.time)`:
  - `instance` (from `resolve_instance_name`) and `url` (from `resolve_advertise_url`), a `PeerDirectory(cfg.directory, instance, url, cfg.token, ...)` and a `PeerClient(cfg.token)`; the token is added to the redactor at construction.
  - `async start()`: `await directory.prepare()` (a `PeersError` propagates) and starts the heartbeat task. `async stop(drain_timeout)`: drains every registered inbox's outcome tasks (`PeerInbox.drain`) within `drain_timeout`, cancels the heartbeat, awaits it, closes the client.
  - `async register(session_id, router, *, name, cwd, started_at) -> PeerInbox`: builds `PeerInbox(router, max_items=cfg.inbox_max, max_per_minute=cfg.max_messages_per_minute, metrics=..., on_outcome=self._notify)`, sets `router.peer_inbox`, publishes the directory entry, and returns the inbox. `async unregister(session_id)`: removes the entry and forgets the session (the router closes the inbox; its dropped items' outcomes still reach `_notify`).
  - `PeerTools` (mcp_server): `async list_peers(caller_session, instance)` -> `[{address, instance, session_id, name, cwd}]` from `await directory.live(exclude=<caller address>, instance=...)`; `async send(caller_session, to, message, notify_when_idle) -> msg_id`: validates the address (`invalid`) and that it is not the caller (`invalid`), checks the UTF-8 size against `max_message_bytes` (`too_large`), looks `to` up live (`unknown_peer`), sends a `MessageBody` through the client (its `PeerError` propagates), emits `peer_message_sent` `{msg_id, to, kind: "message", bytes, notify_when_idle, result}` on the caller's router with result `ok` or the error code, and counts `peer_sent("message", result)`.
  - `PeerReceiver` (api.peers): `accept_message(body)` -> `PeerError("not_found")` unless `body.to_session` is registered here; else `inbox.put(InboxItem(kind="message", ...))` and returns `{msg_id, queued}` (plus `duplicate`). `accept_notice(body)` the same with `kind="notice"`, `status`, `state` and `reason`.
  - `_notify(item, status, record)`: looks the sender address (`item.from_address`) up live; if found sends a `NoticeBody {from: <the receiving session's address>, to_session: <sender session>, msg_id, status, state: record.state for delivered else None, reason}` to the sender's url, emits `peer_message_sent` with `kind: "notice"` on the receiving session's router (when still open) and counts it; a missing entry or a `PeerError` is logged (redacted) and counted, never raised or re-queued.
- tests/test_peer_service.py, two PeerService objects on one tmp directory with an `httpx.MockTransport` routing each instance's url to the other's `create_app` (signed requests end to end), stub routers from the test_router harness where a session is needed: start sweeps our own dir; register publishes and unregister removes; list_peers excludes the caller and filters; send success emits the sent event and the target inbox gets the item; each failure (bad address, self, too large, unknown peer, target inbox full, target rate limited, unreachable) maps to its PeerError and a counted result; accept_message for an unknown session is not_found; a notify_when_idle message gets exactly one delivered notice back, a vetoed message a rejected notice, and a message dropped by unregister/close a dropped notice, each arriving in the sender's inbox; `stop()` drains a pending notice within its timeout; the token never appears in logs, events or captured requests (caplog, event log scan, transport capture).

Interfaces: `peer_service.PeerService` (implements `mcp_server.PeerTools` and `api.peers.PeerReceiver`; the `peer-service` part).

## Acceptance conditions

- AC1: start sweeps our own directory and starts the heartbeat, stop drains and closes [evidence: test:tests/test_peer_service.py::test_start_and_stop]
- AC2: register attaches an inbox to the router and publishes the entry, unregister removes it [evidence: test:tests/test_peer_service.py::test_register_and_unregister]
- AC3: list_peers excludes the caller and filters by instance [evidence: test:tests/test_peer_service.py::test_list_peers]
- AC4: a successful signed send reaches the target inbox and emits peer_message_sent with result ok [evidence: test:tests/test_peer_service.py::test_send_reaches_target_inbox]
- AC5: bad address, self address, too large, unknown peer, inbox full, rate limited and unreachable each raise their PeerError and are counted [evidence: test:tests/test_peer_service.py::test_send_failures]
- AC6: accept_message for a session not registered here raises not_found [evidence: test:tests/test_peer_service.py::test_accept_unknown_session]
- AC7: delivered with notify_when_idle, rejected and dropped messages each send exactly one notice that lands in the sender inbox [evidence: test:tests/test_peer_service.py::test_notices_round_trip]
- AC8: the peer token never appears in logs, events or captured requests [evidence: test:tests/test_peer_service.py::test_token_never_exposed]

## Files

- `src/claude_code_wrapper/peer_service.py`
- `tests/test_peer_service.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
