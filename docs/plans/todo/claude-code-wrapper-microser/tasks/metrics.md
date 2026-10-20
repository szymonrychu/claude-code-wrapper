---
title: Metrics
status: todo
repo: claude-code-wrapper
value: "Operators can scrape session, turn, tool and hook metrics"
files: [src/claude_code_wrapper/metrics.py, src/claude_code_wrapper/router.py, src/claude_code_wrapper/manager.py, src/claude_code_wrapper/api/app.py, tests/test_metrics.py]
verify: "uv run pytest tests/test_metrics.py tests/test_router.py tests/test_manager.py -q && mise run lint"
depends_on: [api-turns-events]
owns: [metrics]
uses: [public-api, event-router]
model: ""
estimate: 60
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Metrics

## Requirement

Add Prometheus metrics (D18): `src/claude_code_wrapper/metrics.py`, instrumentation in `router.py` and `manager.py`, the `/metrics` route in `api/app.py`.

- `Metrics(registry=None)` holding its own `CollectorRegistry` (tests create isolated instances): `ccw_sessions_active` gauge; `ccw_sessions_created_total{result}` (ok, failed, vetoed); `ccw_turns_total{origin,state}`; `ccw_turn_duration_seconds` histogram; `ccw_tool_calls_total{tool,decision}` with the tool label clamped (the first 50 distinct names keep their name, later ones become `other`); `ccw_hook_actions_total{event,type,result}`; `ccw_claude_restarts_total`; `ccw_turn_tokens_total{model,kind}` (input, output, cache_read, cache_creation).
- The manager updates session gauges/counters; the router updates turn, tool, hook and token metrics; both take an optional `Metrics` (None = no-op).
- `GET /metrics` returns the text exposition format (`text/plain; version=0.0.4`), unauthenticated.

## Acceptance conditions

- AC1: /metrics exposes every ccw_ family unauthenticated [evidence: test:tests/test_metrics.py::test_metrics_endpoint_exposes_families]
- AC2: Turn and tool counters increment when the router handles events [evidence: test:tests/test_metrics.py::test_turn_and_tool_counters_increment]
- AC3: The tool label is clamped after 50 distinct names [evidence: test:tests/test_metrics.py::test_tool_label_cardinality_clamped]
- AC4: Hook action outcomes are counted [evidence: test:tests/test_metrics.py::test_hook_action_counter]
- AC5: Session gauges follow create and destroy [evidence: test:tests/test_metrics.py::test_session_gauge_follows_lifecycle]

## Files

- `src/claude_code_wrapper/metrics.py`
- `src/claude_code_wrapper/router.py`
- `src/claude_code_wrapper/manager.py`
- `src/claude_code_wrapper/api/app.py`
- `tests/test_metrics.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
