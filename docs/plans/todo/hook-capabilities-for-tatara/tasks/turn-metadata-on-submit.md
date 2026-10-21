---
title: Turn metadata on submit
status: todo
repo: .
value: "Clients can attach metadata to a turn for hooks to gate on, e.g. tatara handoff (K6)"
files: [src/claude_code_wrapper/api/turns.py, src/claude_code_wrapper/router.py, src/claude_code_wrapper/session.py, src/claude_code_wrapper/turns.py, README.md, tests/test_turn_metadata.py]
verify: "uv run pytest tests/test_turn_metadata.py tests/test_api_turns.py tests/test_turns.py -q && mise run lint"
depends_on: [agent-attribution-and-usage]
owns: [turn-metadata]
uses: [ccw-world-fixture]
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

# Turn metadata on submit

## Requirement

K6 turn metadata (design D10). Owns part `turn-metadata`.

- `POST /v1/sessions/{id}/turns` accepts `metadata` (a JSON object; optional). Serialised above 16 KiB: 413 `too_large`. Not an object: 422 (validation). `TurnRequest` stays `extra="forbid"`.
- `SessionRouter.submit_turn(..., metadata=...)` passes it into the onTurn data as `metadata` (always present, `{}` when absent) and to `Session.submit(..., metadata=...)`.
- `TurnRecord.metadata: dict[str, Any]` (default `{}`), persisted, served by GET turn, present in turn events and callbacks. Initial and agent-origin turns have `{}`. Old turns.jsonl lines load.
- README: the request field and its cap under `### \`POST /v1/sessions/{id}/turns\``, the record field, and `metadata` in the onTurn data list.

## Acceptance conditions

- AC1: metadata reaches onTurn data and the persisted record [evidence: test:tests/test_turn_metadata.py::test_metadata_reaches_on_turn_and_record]
- AC2: metadata above 16 KiB is rejected with 413 too_large [evidence: test:tests/test_turn_metadata.py::test_metadata_too_large_413]
- AC3: non-object metadata is rejected with 422 [evidence: test:tests/test_turn_metadata.py::test_metadata_must_be_object]
- AC4: an onTurn gate can veto on metadata (403 vetoed) [evidence: test:tests/test_turn_metadata.py::test_on_turn_veto_on_metadata]
- AC5: a turn without metadata behaves as before with an empty metadata object [evidence: test:tests/test_turn_metadata.py::test_turn_without_metadata_unchanged]

## Files

- `src/claude_code_wrapper/api/turns.py`
- `src/claude_code_wrapper/router.py`
- `src/claude_code_wrapper/session.py`
- `src/claude_code_wrapper/turns.py`
- `README.md`
- `tests/test_turn_metadata.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
