---
title: Peers two instance e2e
status: in-progress
repo: .
value: "Proves seed join, gossip, messages both ways, notices and leave between three real ccw services"
files: [tests/test_peers_e2e.py]
verify: "uv run pytest tests/test_peers_e2e.py -q && mise run lint"
depends_on: [peers-join-leave-order, fake-claude-peer-harness]
owns: []
uses: [fake-match-substitution, ccw-service-fixture]
model: sonnet
estimate: 75
actual: ""
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/cross-container-peer-messagi--peers-two-instance-e2e
branch: cross-container-peer-messagi--peers-two-instance-e2e
verified_head: ""
run: ""
provides: []
---

# Peers two instance e2e

## Requirement

End-to-end proof in one process with three ccw services on real HTTP ports (design D5, D12). Test-only task: tests/test_peers_e2e.py. A defect found in shipped code is fixed in the owning module within this task and named in the result.

- Three services from `World.service_cfg("a" | "b" | "c", ...)`, started in order with the `ccw_service` fixture, all with `peers: {enabled: true, name: a|b|c, advertise_url: "http://127.0.0.1:{port}", token: <same 16+ char token>, heartbeat_seconds: 0.3, max_missed_heartbeats: 2, join_timeout_seconds: 2}` and the fake claude. A has no seeds; B's `static` is A's url; C's `static` is B's url only.
- One scenario for all, anchored on the message body so the exchange cannot loop (using `{{name}}` substitution): `^send (?P<body>ping) to (?P<to>\S+)$` calls `mcp__ccw__send_message {to: "{{to}}", message: "{{body}}", notify_when_idle: true}`; `<agent-message from="(?P<from>[^"]+)" msg_id="[^"]+">\nping\n</agent-message>` calls `mcp__ccw__send_message {to: "{{from}}", message: "pong"}`; `^list peers$` calls `mcp__ccw__peers {}`. A `pong` body and the notices match no sending turn.
- Flow through the public APIs (bearer `server.auth_token` of each service), tool results read from each session's `<sessions_root>/<id>/config/fake-mcp.jsonl` and `tool_result` events:
  - Create one session per service. Within a bounded wait, A's `list peers` lists the B and C sessions (C reached A only through gossip) and not A's own.
  - `send ping to c/<C id>` on A: C gets a peer turn framed from `a/<A id>` with `ping`; A gets a peer turn with `pong` from `c/<C id>`; A gets the `<peer-idle-notice from="c/<C id>" ... state="completed"/>` turn. Each side sent exactly one message (no loop), checked after a 2 s quiet period.
  - The reverse direction: `send ping to a/<A id>` on C works the same way.
  - A's events show `peer_member_joined` for b and c; sent, received and delivered events and the counters appear on the right services.
  - Stopping C: A's listing drops C promptly (within one heartbeat interval, through its `leaving: true` heartbeat) and A gets `peer_member_left` with reason `leave`.
  - A send to `b/ses_000000000000` gives a tool result starting `unknown peer`; `POST /v1/peers/heartbeat` to B with a bearer token and no signature is 401, and `POST /v1/peers/join` and `POST /v1/peers/leave` are 404.
- Every wait is bounded (at most 30 s each) and names what it waited for.

Interfaces: none new; consumes every part of the epic through the public and internal APIs.

## Acceptance conditions

- AC1: with B seeded by A and C seeded by B, A lists the sessions of B and C and not its own [evidence: test:tests/test_peers_e2e.py::test_gossip_listing_across_services]
- AC2: a ping from A reaches C as a framed peer turn and C's pong reaches A, once each [evidence: test:tests/test_peers_e2e.py::test_send_and_reply_round_trip]
- AC3: the reverse direction from C to A works the same way [evidence: test:tests/test_peers_e2e.py::test_reverse_direction]
- AC4: notify_when_idle delivers an idle notice turn to the sender after the handling turn completes [evidence: test:tests/test_peers_e2e.py::test_notify_when_idle_round_trip]
- AC5: membership and message events and counters appear on the right services [evidence: test:tests/test_peers_e2e.py::test_events_and_metrics]
- AC6: stopping C drops it from A's listing through its leaving heartbeat with a peer_member_left event [evidence: test:tests/test_peers_e2e.py::test_stopped_service_leaves]
- AC7: an unknown address gives an unknown peer tool result and an unsigned heartbeat is 401 [evidence: test:tests/test_peers_e2e.py::test_unknown_peer_and_unsigned_heartbeat]

## Files

- `tests/test_peers_e2e.py`


## Interfaces

See the Requirement's Interfaces paragraph; keep every name and signature stated there.

## Expected outcome

Three ccw services on real ports join by seed and gossip, list each other, exchange messages both ways with notices, and drop a leaver, through public APIs only.

## Steps

- Write the tests named in the acceptance conditions in `tests/test_peers_e2e.py` first; run `uv run pytest tests/test_peers_e2e.py -q` and see them fail for the missing behaviour (not an import typo).
- Implement the smallest change that makes them pass; run `uv run pytest tests/test_peers_e2e.py -q` again and expect all passed.
- Run the task verify: `uv run pytest tests/test_peers_e2e.py -q && mise run lint`; expect all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

Test-only task; a defect found in shipped code is fixed in its module here and named in the result. Every wait is bounded.
