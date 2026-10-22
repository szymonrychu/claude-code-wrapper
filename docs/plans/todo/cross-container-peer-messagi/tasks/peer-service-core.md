---
title: Peer service core
status: todo
repo: .
value: "One object per ccw instance runs discovery, sending, receiving and idle notices for every session"
files: [src/claude_code_wrapper/peer_service.py, tests/test_peer_service.py]
verify: "uv run pytest tests/test_peer_service.py -q && mise run lint"
depends_on: [peer-inbox-delivery, peer-messages-endpoint, peers-mcp-tools, peer-directory-module, peer-http-client, peer-events-and-metrics]
owns: [peer-service]
uses: [peers-config, peer-directory-entry, peer-wire, peer-inbox, peer-messages-endpoint, ccw-mcp-peers-tool, peer-events, peer-metrics]
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
  - `instance` (from `resolve_instance_name`) and `url` (from `resolve_advertise_url`), a `PeerDirectory` on `cfg.directory` and a `PeerClient(cfg.token)`; the token is added to the redactor at construction.
  - `async start()`: `directory.prepare()` (a `PeersError` propagates) and starts the heartbeat task. `async stop()`: cancels the heartbeat, awaits it, closes the client.
  - `register(session_id, router, *, name, cwd, started_at) -> PeerInbox`: builds a `PeerInbox(router, max_items=cfg.inbox_max, metrics=..., on_handled=self._notify)`, sets `router.peer_inbox`, publishes the directory entry, and returns the inbox. `unregister(session_id)`: removes the entry and forgets the session (the router closes the inbox).
  - `PeerTools` (mcp_server): `list_peers(caller_session, instance)` -> `[{address, instance, session_id, name, cwd}]` from `directory.live(exclude=<caller address>, instance=...)`; `async send(caller_session, to, message, notify_when_idle) -> msg_id`: validates the address (`invalid`) and that it is not the caller (`invalid`), checks the UTF-8 size against `max_message_bytes` (`too_large`), looks `to` up live (`unknown_peer`), POSTs a `MessageBody` through the client (a target answer's `PeerError` propagates), emits `peer_message_sent` `{msg_id, to, kind: "message", bytes, notify_when_idle, result}` on the caller's router with result `ok` or the error code, and counts `peer_sent("message", result)` for success and every failure.
  - `PeerReceiver` (api.peers): `accept_message(body)` -> `PeerError("not_found")` unless `body.to_session` is registered here; else `inbox.put(InboxItem(kind="message", ...))` and returns `{msg_id, queued}` (plus `duplicate`). `accept_notice(body)` the same with `kind="notice"` and `state`.
  - `_notify(item, record)`: for a handled `notify_when_idle` message, looks the sender address up live; if found POSTs a `NoticeBody {from: <this session's address>, to_session: <sender session>, msg_id, state: record.state}` to the sender's url, emits `peer_message_sent` with `kind: "notice"` on the handling session's router and counts it; a missing entry or a `PeerError` is logged (redacted) and counted, never raised or retried.
- tests/test_peer_service.py, using two PeerService objects on one tmp directory with an `httpx.MockTransport` that routes each instance's url to the other's `create_app` (or direct calls to `accept_message`), stub routers from the test_router harness where a session is needed: start sweeps and refuses a live name clash; register publishes and unregister removes; list_peers excludes the caller and filters; send success emits the sent event and the target inbox gets the item; each failure (bad address, self, too large, unknown peer, target inbox full, unreachable) maps to its PeerError and a counted, logged result; accept_message for an unknown session is not_found; a notify_when_idle message produces exactly one notice to the sender after the handling turn ends and none for a plain message; the token never appears in logs or events (caplog plus event log scan).

Interfaces: `peer_service.PeerService` (implements `mcp_server.PeerTools` and `api.peers.PeerReceiver`; the `peer-service` part).

## Acceptance conditions

- AC1: start prepares the directory and starts the heartbeat, and a live name clash raises PeersError [evidence: test:tests/test_peer_service.py::test_start_prepares_and_refuses_clash]
- AC2: register attaches an inbox to the router and publishes the entry, unregister removes it [evidence: test:tests/test_peer_service.py::test_register_and_unregister]
- AC3: list_peers excludes the caller and filters by instance [evidence: test:tests/test_peer_service.py::test_list_peers]
- AC4: a successful send reaches the target inbox and emits peer_message_sent with result ok [evidence: test:tests/test_peer_service.py::test_send_reaches_target_inbox]
- AC5: bad address, self address, too large, unknown peer, inbox full and unreachable each raise their PeerError and are counted [evidence: test:tests/test_peer_service.py::test_send_failures]
- AC6: accept_message for a session not registered here raises not_found [evidence: test:tests/test_peer_service.py::test_accept_unknown_session]
- AC7: a notify_when_idle message yields exactly one idle notice to the sender after its turn ends, a plain message none [evidence: test:tests/test_peer_service.py::test_idle_notice_round_trip]
- AC8: the peer token never appears in logs or events [evidence: test:tests/test_peer_service.py::test_token_never_logged]

## Files

- `src/claude_code_wrapper/peer_service.py`
- `tests/test_peer_service.py`

## Interfaces

See the Requirement's Interfaces paragraph; keep every name and signature stated there.

## Expected outcome

One PeerService per instance publishes sessions, lists and sends for the MCP tools, receives for the HTTP routes and sends idle notices.

## Steps

- Write the tests named in the acceptance conditions in `tests/test_peer_service.py` first; run `uv run pytest tests/test_peer_service.py -q` and see them fail for the missing behaviour (not an import typo).
- Implement the smallest change that makes them pass; run `uv run pytest tests/test_peer_service.py -q` and expect all passed.
- Run the task verify: `uv run pytest tests/test_peer_service.py -q && mise run lint`; expect all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

Review is full: shared state across sessions, background heartbeat and a secret token. Keep the token out of every log line and event.
