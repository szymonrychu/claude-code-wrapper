---
title: Peer events and metrics
status: todo
repo: .
value: Operators can watch peer traffic on the SSE stream and in Prometheus
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

Event types and metric families for peer messaging (design D9). Emission is done by later tasks; this task adds the vocabulary, the recorders and the docs.

- src/claude_code_wrapper/events.py: `EVENT_TYPES` gains `peer_message_received`, `peer_message_sent`, `peer_message_delivered`, inserted after `destroy_output` and before `session_closed`, so `session_closed` stays last.
- tests/test_event_log.py: the exact-list pin is updated to the new list (not loosened to a subset check).
- src/claude_code_wrapper/metrics.py: counters `ccw_peer_messages_received_total{kind,result}`, `ccw_peer_messages_sent_total{kind,result}`, `ccw_peer_messages_delivered_total{kind,result}` with recorder methods `Metrics.peer_received(kind, result)`, `Metrics.peer_sent(kind, result)`, `Metrics.peer_delivered(kind, result)`. `kind` is `message` or `notice`; any other kind raises `ValueError` (my call: a typo must not mint a label).
- README.md: under `## Events (SSE)` one `### \`<type>\`` heading per new type with its `data` keys exactly as design D9 lists them (`peer_message_received`: msg_id, from, kind, bytes, message, queued; `peer_message_sent`: msg_id, to, kind, bytes, notify_when_idle, result; `peer_message_delivered`: msg_id, from, kind, turn_id, result with delivered, rejected or dropped); under `### \`GET /metrics\`` the three families with their labels and result values (received: accepted, duplicate, inbox_full, rate_limited, too_large, not_found, unauthorized; delivered: delivered, rejected, dropped; sent: ok or the PeerError code).
- tests/test_peer_metrics.py: each recorder increments its family with the given labels (read through `Metrics.render()` or the registry), and a bad kind raises.

Interfaces: the three EVENT_TYPES names (the `peer-events` part; cross-epic `event-types` contract owned by hook-capabilities-for-tatara) and the three `Metrics` recorder methods (the `peer-metrics` part).

## Acceptance conditions

- AC1: EVENT_TYPES has the three peer types before session_closed and the pin in test_event_log matches it exactly [evidence: command:uv run pytest tests/test_event_log.py -q]
- AC2: the three peer counters increment with kind and result labels [evidence: test:tests/test_peer_metrics.py::test_peer_counters]
- AC3: an unknown kind raises ValueError [evidence: test:tests/test_peer_metrics.py::test_bad_kind_rejected]
- AC4: every new event type and metric family is documented in the README [evidence: command:uv run pytest tests/test_readme_api.py tests/test_metrics.py tests/test_metric_families.py -q]

## Files

- `src/claude_code_wrapper/events.py`
- `src/claude_code_wrapper/metrics.py`
- `tests/test_event_log.py`
- `tests/test_peer_metrics.py`
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
