---
title: Turn metadata on submit
status: in-progress
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

- Part `turn-metadata`: `api/turns.py::TurnRequest.metadata`, `turns.py::TurnRecord.metadata`, `SessionRouter.submit_turn(..., metadata=...)`, `Session.submit(..., metadata=...)`, onTurn `data.metadata`.

## Expected outcome

A client can send per-turn metadata (e.g. `{"handoff": true}`) that onTurn gates read and that is kept on the record.

## Steps

- Write `tests/test_turn_metadata.py` (API via the app with a `world` manager, plus an onTurn gate script); see it fail.
- Add the field through API, router, session and record; the 16 KiB cap.
- README.
- `uv run pytest tests/test_turn_metadata.py tests/test_api_turns.py tests/test_turns.py -q && mise run lint`.

## Notes

- Keep `extra="forbid"` on `TurnRequest`; only `metadata` is added.
