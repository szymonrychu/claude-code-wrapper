---
title: Turn annotations from hooks
status: done
repo: .
value: "Hooks can tag a turn record (pending tasks, outcomes) that clients see in GET turn and callbacks (K2)"
files: [src/claude_code_wrapper/turns.py, src/claude_code_wrapper/router.py, src/claude_code_wrapper/session.py, README.md, tests/test_turn_annotations.py]
verify: "uv run pytest tests/test_turn_annotations.py tests/test_turns.py tests/test_router.py -q && mise run lint"
depends_on: [stop-gate-onstop-event]
owns: [turn-annotations]
uses: [hook-result-fields, on-stop-event, ccw-world-fixture]
model: sonnet
estimate: 45
actual: 5
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/hook-capabilities-for-tatara--turn-annotations-from-hooks
branch: hook-capabilities-for-tatara--turn-annotations-from-hooks
verified_head: b1cb4fa106cb2ac2c62c630058a3a804e0a7a202
run: hook-capabilities-for-tatara
provides: [turn-annotations]
acs_for_audit: []
process_gaps: ["The implementer says the implementation was written before the tests, so there is no red run, which breaks the test-first rule."]
---

# Turn annotations from hooks

## Requirement

K2 hook annotations (design D6). Owns part `turn-annotations`.

- `TurnRecord.annotations: dict[str, Any]` (default `{}`), persisted in turns.jsonl; old lines without it load. It appears in `GET /v1/sessions/{id}/turns/{turn_id}`, in `turn_completed`/`turn_failed` events and therefore in callbacks.
- The router shallow-merges `HookOutcome.annotations` (later keys win) into the running turn's record for the turn-scoped events: `onTurn` (applied to the new record at submit: `Session.submit` takes `annotations`), `onAgentToolCall`, `onToolResult`, `onStop` (merged on every leg - allow, block and defer - before any completion, so they are in the `turn_completed` event and the callback; this task owns the merge call in the router `_on_stop`, stop-gate-onstop-event only produces the outcome) and `onTurnComplete` (merged into the terminal record and persisted again; visible in GET turn; the callback already sent is not re-sent, documented).
- Annotations from any other event are ignored. A merge that would take a turn's annotations above 16 KiB serialised is dropped and an `error` event (kind `annotations_too_large`) is emitted.
- README: the `annotations` field in the GET turn record and a paragraph in `## Hooks` on which events annotate the turn.

## Acceptance conditions

- AC1: onTurn annotations land on the new record [evidence: test:tests/test_turn_annotations.py::test_on_turn_annotations_on_record]
- AC2: onAgentToolCall and onToolResult annotations merge into the running turn [evidence: test:tests/test_turn_annotations.py::test_tool_hook_annotations_merged]
- AC3: onStop annotations are in the turn_completed event and the callback [evidence: test:tests/test_turn_annotations.py::test_on_stop_annotations_in_callback]
- AC4a: onStop annotations are merged into the turn record on the block and defer legs too [evidence: test:tests/test_turn_annotations.py::test_on_stop_annotations_merged_on_block_and_defer_legs]
- AC4: onTurnComplete annotations are persisted and served by GET turn [evidence: test:tests/test_turn_annotations.py::test_on_turn_complete_annotations_persisted]
- AC5: annotations from non turn-scoped events are ignored [evidence: test:tests/test_turn_annotations.py::test_non_turn_event_annotations_ignored]
- AC6: a merge past 16 KiB is dropped with an error event [evidence: test:tests/test_turn_annotations.py::test_annotations_size_cap]
- AC7: turns.jsonl lines without annotations still load [evidence: test:tests/test_turn_annotations.py::test_old_turns_jsonl_loads_without_annotations]

## Files

- `src/claude_code_wrapper/turns.py`
- `src/claude_code_wrapper/router.py`
- `src/claude_code_wrapper/session.py`
- `README.md`
- `tests/test_turn_annotations.py`

## Interfaces

- Part `turn-annotations`: `turns.py::TurnRecord.annotations`; `Session.submit(..., annotations=...)`; router merge helper.

## Expected outcome

Hooks on turn-scoped events can tag the turn (e.g. `pending_tasks`, outcome state) and clients read the tags from GET turn and the turn callback.

## Steps

- Write `tests/test_turn_annotations.py` (`world` fixture, hooks printing JSON with annotations); see it fail.
- Add the field, the merge in each turn-scoped handler, the size cap.
- README record field and hooks paragraph.
- `uv run pytest tests/test_turn_annotations.py tests/test_turns.py tests/test_router.py -q && mise run lint`.

## Notes

- `HookOutcome.annotations` is already merged across actions by hook-result-fields; merge it into the record, later keys win.
