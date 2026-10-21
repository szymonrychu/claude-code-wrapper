---
title: Command event vocabulary
status: todo
repo: .
value: "Clients and hooks can observe and veto commands and see every compaction, with bounded metrics"
files: [src/claude_code_wrapper/config.py, src/claude_code_wrapper/events.py, src/claude_code_wrapper/metrics.py, README.md, tests/test_command_vocabulary.py, tests/test_event_log.py, tests/test_config.py]
verify: "uv run pytest tests/test_command_vocabulary.py tests/test_event_log.py tests/test_config.py tests/test_hook_result_fields.py tests/test_e2e_lifecycle.py tests/test_metrics.py tests/test_metric_families.py tests/test_readme_api.py -q && mise run lint"
depends_on: [command-registry-and-config]
owns: [command-events, command-hooks, command-metrics]
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

# Command event vocabulary

## Requirement

Event types, hook events and metric families for commands and compaction (design D7). Vocabulary only: emitting and firing them is done by tasks command-completion-signals and command-runner. Owns parts `command-events` (C/events.py), `command-hooks` (C/config.py) and `command-metrics` (C/metrics.py).

- `events.EVENT_TYPES` gains `command_queued`, `command_started`, `command_completed`, `command_failed`, `compacted`, in that order between `hook_result` and `error`; tests/test_event_log.py::test_event_types_exact pins the new list.
- `config.HOOK_EVENTS` gains `onCommand` and `onCommandComplete` before `onError` (`HooksConfig` gets both fields, so `hooks.onCommand` and `hooks.onCommandComplete` validate); `VETO_EVENTS` gains `onCommand`, so its `deny` decision counts (EVENT_DECISIONS is derived from VETO_EVENTS); `onCommandComplete` is not a veto event. tests/test_config.py pins move to 16 hook events and the new VETO_EVENTS tuple.
- `metrics.Metrics`: `ccw_commands_total{command,origin,result}` (result = the final state: completed, failed, cancelled), `ccw_command_duration_seconds{command}` (histogram), `ccw_compactions_total{trigger}`; methods `command_finished(command, origin, result, seconds)` and `compaction(trigger)`. Labels stay bounded: `command` is a built-in name (anything else counts as `other`), `trigger` is `manual`, `auto` or `unknown` (anything else counts as `unknown`).
- README: `### command_queued`, `### command_started`, `### command_completed`, `### command_failed` (data `{record}`: the CommandRecord fields), `### compacted` (data `{trigger, pre_tokens, post_tokens, duration_ms}`, emitted for every compaction including Claude Code's own), `### onCommand` (blocking, can deny; data `{command_id, name, args, origin, turn_id}`; a deny fails the command with error `vetoed`), `### onCommandComplete` (data = the final record), and the three metric families in `### GET /metrics`.

Interfaces: the five event type names, `onCommand`/`onCommandComplete` in `HOOK_EVENTS`, `onCommand` in `VETO_EVENTS`, `Metrics.command_finished`, `Metrics.compaction`.

Notes: tests/test_e2e_lifecycle.py builds a hook for every HOOK_EVENTS entry; keep it green (the new events simply never fire there). tests/test_readme_api.py requires a README section for every event type and hook event.

## Acceptance conditions

- AC1: EVENT_TYPES lists the five new events between hook_result and error [evidence: test:tests/test_event_log.py::test_event_types_exact]
- AC2: hooks.onCommand accepts a deny decision and onCommandComplete does not count deny [evidence: test:tests/test_command_vocabulary.py::test_on_command_is_a_veto_event]
- AC3: HOOK_EVENTS has 16 entries ending in onError and VETO_EVENTS includes onCommand [evidence: test:tests/test_command_vocabulary.py::test_hook_events_include_command_hooks]
- AC4: the three command metric families render with their labels [evidence: test:tests/test_command_vocabulary.py::test_command_metric_families_render]
- AC5: command and trigger labels are bounded [evidence: test:tests/test_command_vocabulary.py::test_metric_labels_bounded]
- AC6: README has a section for every new event and hook and lists the metric families [evidence: command:uv run pytest tests/test_readme_api.py -q]
- AC7: existing config, hook and e2e lifecycle tests stay green [evidence: command:uv run pytest tests/test_config.py tests/test_hook_result_fields.py tests/test_e2e_lifecycle.py tests/test_metrics.py tests/test_metric_families.py -q]

## Files

- `src/claude_code_wrapper/config.py`
- `src/claude_code_wrapper/events.py`
- `src/claude_code_wrapper/metrics.py`
- `README.md`
- `tests/test_command_vocabulary.py`
- `tests/test_event_log.py`
- `tests/test_config.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
