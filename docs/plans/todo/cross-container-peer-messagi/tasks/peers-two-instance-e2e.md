---
title: Peers two instance e2e
status: todo
repo: .
value: "Proves a full send, reply and idle notice between two real ccw services"
files: [tests/test_peers_e2e.py]
verify: "uv run pytest tests/test_peers_e2e.py -q && mise run lint"
depends_on: [manager-peers-lifecycle, fake-claude-peer-harness]
owns: []
uses: [fake-match-substitution, ccw-service-fixture]
model: sonnet
estimate: 60
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Peers two instance e2e

## Requirement

End-to-end proof in one process with two ccw services on real HTTP ports (design D12). Test-only task: tests/test_peers_e2e.py. A defect found in shipped code is fixed in the owning module within this task and named in the result.

- Two services from `World.service_cfg("a", ...)` and `World.service_cfg("b", ...)`, started with the `ccw_service` fixture, both with `peers: {enabled: true, name: a|b, advertise_url: "http://127.0.0.1:{port}", token: <same 16+ char token>, directory: <one shared tmp dir>, heartbeat_seconds: 0.5, stale_seconds: 3}` and the fake claude.
- Scenarios (using the `{{name}}` substitution): A's turn `send to (?P<to>\S+)` calls `mcp__ccw__send_message {to: "{{to}}", message: "ping", notify_when_idle: true}`; B's turn `<agent-message from="(?P<from>[^"]+)"` calls `mcp__ccw__send_message {to: "{{from}}", message: "pong"}`; A's turn `list peers` calls `mcp__ccw__peers {}`.
- Flow through the public APIs only (bearer `server.auth_token` of each service): create a session in B and in A; A's `list peers` turn result lists `b/<B id>` and not A; submit `send to b/<B id>` to A; then:
  - B gets a turn with origin `peer` whose text is the D7 frame from `a/<A id>` with `ping`.
  - A gets a turn with origin `peer` whose text is the frame from `b/<B id>` with `pong`.
  - A gets a turn with origin `peer` whose text is the `<peer-idle-notice from="b/<B id>" msg_id="..." state="completed"/>` frame for the ping's msg_id.
  - A's event stream has `peer_message_sent` (result ok) and B's has `peer_message_received` and `peer_message_delivered`; `GET /metrics` on each shows the matching counters.
- A send to `b/ses_000000000000` (unknown) returns an error result starting `unknown peer` in A's transcript; a direct `POST /v1/peers/messages` to B with A's `server.auth_token` is 401.
- Stopping B removes its directory entries, and A's `list peers` no longer lists it.
- Every wait is bounded (at most 30 s each) and names what it waited for.

Interfaces: none new; consumes every part of the epic through the public and internal APIs.

## Acceptance conditions

- AC1: A's peers tool lists B's session address and not its own [evidence: test:tests/test_peers_e2e.py::test_peers_listing_across_services]
- AC2: A's send_message reaches B as a framed peer turn and B's reply reaches A as a framed peer turn [evidence: test:tests/test_peers_e2e.py::test_send_and_reply_round_trip]
- AC3: notify_when_idle delivers an idle notice turn to A after B's handling turn completes [evidence: test:tests/test_peers_e2e.py::test_notify_when_idle_round_trip]
- AC4: the sent, received and delivered events and counters appear on the right services [evidence: test:tests/test_peers_e2e.py::test_events_and_metrics]
- AC5: an unknown address gives an unknown peer error result and server.auth_token cannot post to a peer route [evidence: test:tests/test_peers_e2e.py::test_unknown_peer_and_token_separation]
- AC6: stopping B removes it from A's listing [evidence: test:tests/test_peers_e2e.py::test_stopped_service_disappears]

## Files

- `tests/test_peers_e2e.py`

## Interfaces

See the Requirement's Interfaces paragraph; keep every name and signature stated there.

## Expected outcome

Two ccw services on real ports complete list, send, reply and idle notice with the fake claude, through public APIs only.

## Steps

- Write the tests named in the acceptance conditions in `tests/test_peers_e2e.py` first; run `uv run pytest tests/test_peers_e2e.py -q` and see them fail for the missing behaviour (not an import typo).
- Implement the smallest change that makes them pass; run `uv run pytest tests/test_peers_e2e.py -q` and expect all passed.
- Run the task verify: `uv run pytest tests/test_peers_e2e.py -q && mise run lint`; expect all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

Test-only task; a defect found in shipped code is fixed in its module here and named in the result. Every wait is bounded.
