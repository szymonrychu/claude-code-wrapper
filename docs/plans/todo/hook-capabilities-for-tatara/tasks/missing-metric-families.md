---
title: Missing metric families
status: todo
repo: .
value: "HTTP, probe, stall and interrupt metrics that tatara alerts depend on (K13)"
files: [src/claude_code_wrapper/metrics.py, src/claude_code_wrapper/api/app.py, src/claude_code_wrapper/api/turns.py, src/claude_code_wrapper/router.py, README.md, tests/test_metric_families.py]
verify: "uv run pytest tests/test_metric_families.py tests/test_metrics.py -q && mise run lint"
depends_on: [destroy-hook-input-output]
owns: []
uses: [probe-alive-token]
model: sonnet
estimate: 75
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Missing metric families

## Requirement

K13 missing metric families (design D17).

- `ccw_http_requests_total{route,method,status_code}`: every public API response, counted by a middleware in `api/app.py`; `route` is the matched route template (e.g. `/v1/sessions/{session_id}/turns`), `unmatched` when no route matched (bounded cardinality).
- `ccw_http_panics_total`: an unhandled exception in a handler (the 500 path), counted by the same middleware.
- `ccw_probe_outcomes_total{outcome}`: `sent`, `rejected` (booting/dead), `delivered`, `answered`.
- `ccw_probe_answer_seconds` histogram: probe `created_at` to `answered_at`.
- `ccw_stall_suspected_total`: each stall report.
- `ccw_interrupts_total{result}`: `interrupted`, `timeout` (turn_max_seconds), `idle` (interrupt with no running turn).
- New `Metrics` methods; wiring in api/app.py (http), api/turns.py (probe sent/rejected), router.py (delivered, answered with seconds, stall, interrupts).
- README metrics list under `### \`GET /metrics\``. Label renames for tatara dashboards are consumer work and out of scope.

## Acceptance conditions

- AC1: http requests are counted by route template, method and status code [evidence: test:tests/test_metric_families.py::test_http_requests_labelled_by_route_template]
- AC2: an unhandled handler exception counts a panic [evidence: test:tests/test_metric_families.py::test_http_panic_counted]
- AC3: probe outcomes and answer seconds are recorded [evidence: test:tests/test_metric_families.py::test_probe_outcomes_and_answer_seconds]
- AC4: stall reports are counted [evidence: test:tests/test_metric_families.py::test_stall_counted]
- AC5: interrupt results are counted [evidence: test:tests/test_metric_families.py::test_interrupt_results_counted]
- AC6: existing metrics tests stay green and README lists the families [evidence: command:uv run pytest tests/test_metrics.py -q && grep -q ccw_http_requests_total README.md && grep -q ccw_interrupts_total README.md]

## Files

- `src/claude_code_wrapper/metrics.py`
- `src/claude_code_wrapper/api/app.py`
- `src/claude_code_wrapper/api/turns.py`
- `src/claude_code_wrapper/router.py`
- `README.md`
- `tests/test_metric_families.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
