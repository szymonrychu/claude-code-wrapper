---
title: Turn annotations from hooks
status: todo
repo: .
value: "Hooks can tag a turn record (pending tasks, outcomes) that clients see in GET turn and callbacks (K2)"
files: [src/claude_code_wrapper/turns.py, src/claude_code_wrapper/router.py, src/claude_code_wrapper/session.py, README.md, tests/test_turn_annotations.py]
verify: "uv run pytest tests/test_turn_annotations.py tests/test_turns.py tests/test_router.py -q && mise run lint"
depends_on: [stop-gate-onstop-event]
owns: [turn-annotations]
uses: [hook-result-fields, on-stop-event, ccw-world-fixture]
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

# Turn annotations from hooks

## Requirement

K2 hook annotations (design D6). Owns part `turn-annotations`.

- `TurnRecord.annotations: dict[str, Any]` (default `{}`), persisted in turns.jsonl; old lines without it load. It appears in `GET /v1/sessions/{id}/turns/{turn_id}`, in `turn_completed`/`turn_failed` events and therefore in callbacks.
- The router shallow-merges `HookOutcome.annotations` (later keys win) into the running turn's record for the turn-scoped events: `onTurn` (applied to the new record at submit: `Session.submit` takes `annotations`), `onAgentToolCall`, `onToolResult`, `onStop` (merged before completion, so they are in the `turn_completed` event and the callback) and `onTurnComplete` (merged into the terminal record and persisted again; visible in GET turn; the callback already sent is not re-sent, documented).
- Annotations from any other event are ignored. A merge that would take a turn's annotations above 16 KiB serialised is dropped and an `error` event (kind `annotations_too_large`) is emitted.
- README: the `annotations` field in the GET turn record and a paragraph in `## Hooks` on which events annotate the turn.

## Acceptance conditions

- AC1: onTurn annotations land on the new record [evidence: test:tests/test_turn_annotations.py::test_on_turn_annotations_on_record]
- AC2: onAgentToolCall and onToolResult annotations merge into the running turn [evidence: test:tests/test_turn_annotations.py::test_tool_hook_annotations_merged]
- AC3: onStop annotations are in the turn_completed event and the callback [evidence: test:tests/test_turn_annotations.py::test_on_stop_annotations_in_callback]
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

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
