---
title: Peer events and metrics
status: todo
repo: .
value: Operators can watch peer traffic and membership on the SSE stream and in Prometheus
files: [src/claude_code_wrapper/events.py, src/claude_code_wrapper/metrics.py, tests/test_event_log.py, tests/test_peer_metrics.py, README.md]
verify: "uv run pytest tests/test_event_log.py tests/test_peer_metrics.py tests/test_metrics.py tests/test_readme_api.py -q && mise run lint"
depends_on: [peers-config-section]
owns: [peer-events, peer-metrics]
uses: []
model: sonnet
estimate: 45
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Peer events and metrics

## Requirement

Event types and metric families for peer messaging and membership (design D9). Emission is done by later tasks; this task adds the vocabulary, the recorders and the docs.

- src/claude_code_wrapper/events.py: `EVENT_TYPES` gains `peer_message_received`, `peer_message_sent`, `peer_message_delivered`, `peer_member_joined`, `peer_member_left`, inserted after `destroy_output` and before `session_closed`, so `session_closed` stays last.
- tests/test_event_log.py: the exact-list pin is updated to the new list (not loosened).
- src/claude_code_wrapper/metrics.py: counters `ccw_peer_messages_received_total{kind,result}`, `ccw_peer_messages_sent_total{kind,result}`, `ccw_peer_messages_delivered_total{kind,result}`, `ccw_peer_membership_total{op,result}` and the gauge `ccw_peer_members`, with recorders `Metrics.peer_received(kind, result)`, `peer_sent(kind, result)`, `peer_delivered(kind, result)`, `peer_membership(op, result)` and `peer_members(count)`. `kind` is `message` or `notice`; `op` is `join`, `heartbeat`, `leave` (the outbound rounds; all three are heartbeat calls, approval O) or `inbound_heartbeat`; any other kind or op raises `ValueError` (my call: a typo must not mint a label).
- README.md: under `## Events (SSE)` one `### \`<type>\`` heading per new type with its `data` keys exactly as design D9 lists them; under `### \`GET /metrics\`` the five families with their labels and values (received results: accepted, duplicate, inbox_full, rate_limited, too_large, not_found, unauthorized; delivered: delivered, rejected, dropped; sent: ok or the PeerError code; membership results: ok, failed, vetoed).
- tests/test_peer_metrics.py: each recorder updates its family with the given labels (read through `Metrics.render()` or the registry), and a bad kind or op raises.

Interfaces: the five EVENT_TYPES names (the `peer-events` part; cross-epic `event-types` contract owned by hook-capabilities-for-tatara) and the five `Metrics` recorder methods (the `peer-metrics` part).

## Acceptance conditions

- AC1: EVENT_TYPES has the five peer types before session_closed and the pin in test_event_log matches it exactly [evidence: command:uv run pytest tests/test_event_log.py -q]
- AC2: the message counters increment with kind and result labels [evidence: test:tests/test_peer_metrics.py::test_peer_message_counters]
- AC3: the membership counter and members gauge record op, result and count [evidence: test:tests/test_peer_metrics.py::test_peer_membership_metrics]
- AC4: an unknown kind or op raises ValueError [evidence: test:tests/test_peer_metrics.py::test_bad_labels_rejected]
- AC5: every new event type and metric family is documented in the README [evidence: command:uv run pytest tests/test_readme_api.py tests/test_metrics.py tests/test_metric_families.py -q]

## Files

- `src/claude_code_wrapper/events.py`
- `src/claude_code_wrapper/metrics.py`
- `tests/test_event_log.py`
- `tests/test_peer_metrics.py`
- `README.md`


## Interfaces

See the Requirement's Interfaces paragraph; keep every name and signature stated there.

## Expected outcome

The peer event types and metric families exist, are pinned and documented, ready for later tasks to emit.

## Steps

- Write the tests named in the acceptance conditions in `tests/test_peer_metrics.py` first; run `uv run pytest tests/test_peer_metrics.py -q` and see them fail for the missing behaviour (not an import typo).
- Implement the smallest change that makes them pass; run `uv run pytest tests/test_peer_metrics.py -q` again and expect all passed.
- Run the task verify: `uv run pytest tests/test_event_log.py tests/test_peer_metrics.py tests/test_metrics.py tests/test_readme_api.py -q && mise run lint`; expect all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

test_readme_api.py fails the suite for an undocumented event type, so README headings land with the EVENT_TYPES change. Second README-editing task (README order: peers-config-section, peer-events-and-metrics, peer-membership-hooks, peer-messages-endpoint, peers-readme-guide.)
