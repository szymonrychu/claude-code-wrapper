---
title: Hook result fields
status: in-progress
repo: .
value: "Hooks can return annotations and output, the base for stop gates, turn tags and destroy reviews"
files: [src/claude_code_wrapper/hooks.py, src/claude_code_wrapper/router.py, README.md, tests/test_hook_result_fields.py]
verify: "uv run pytest tests/test_hook_result_fields.py tests/test_hooks_command.py tests/test_hooks_webhook.py tests/test_router.py -q && mise run lint"
depends_on: []
owns: [hook-result-fields]
uses: []
model: sonnet
estimate: 60
actual: ""
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/hook-capabilities-for-tatara--hook-result-fields
branch: hook-capabilities-for-tatara--hook-result-fields
verified_head: ""
run: ""
provides: []
---

# Hook result fields

## Requirement

Extend the hook dispatcher's result parsing in `src/claude_code_wrapper/hooks.py` (design D6, D7; foundation for D5). Owns part `hook-result-fields`.

- The decision parser (today `_parse_decision`) reads, from command stdout on exit 0 and from webhook response bodies: `decision` in `allow|deny|block|defer`, `reason`, `additional_context`, `annotations` (a JSON object) and `output` (any JSON value).
- `EVENT_DECISIONS` in hooks.py maps each event to the decisions it honours: the `VETO_EVENTS` honour `allow|deny`, every other event honours `allow` only (it covers the existing events only; stop-gate-onstop-event adds onStop with `allow|block|defer` and ontick-timed-hook adds onTick with `allow` only). A decision the event does not honour is treated as `allow`, and the action's result records it (`invalid_decision: <value>` on the `hook_result` event); it never vetoes.
- `ActionResult` gains `annotations: dict | None` and `output: Any | None`. `HookOutcome` gains `annotations: dict` (shallow merge of the blocking results in action index order, later keys win) and `outputs: list[dict]` (one `{index, ok, output, error}` per blocking action, in index order).
- `output` is redacted with the dispatcher's redactor and capped: serialised JSON above `OUTPUT_CAP_BYTES` (256 KiB) is replaced by `{"truncated": true, "bytes": <n>}`. `annotations` above 16 KiB serialised are dropped and the result records an error.
- Non-blocking actions' `annotations` and `output` are ignored (they cannot affect the outcome).
- `SessionRouter.on_hook_result` includes `output` in the `hook_result` event when it is not None.
- README `## Hooks`, sections `### Command actions` and `### Webhook actions`: document `annotations`, `output`, the per-event decision table and the caps. Behaviour is unchanged when the new fields are absent.

## Acceptance conditions

- AC1: annotations and output are parsed from command stdout [evidence: test:tests/test_hook_result_fields.py::test_parses_annotations_and_output_from_command_stdout]
- AC2: annotations and output are parsed from a webhook response body [evidence: test:tests/test_hook_result_fields.py::test_parses_fields_from_webhook_body]
- AC3: annotations of blocking actions merge in index order, later keys win [evidence: test:tests/test_hook_result_fields.py::test_annotations_merged_in_index_order]
- AC4: outputs lists one entry per blocking action, failures with their error [evidence: test:tests/test_hook_result_fields.py::test_outputs_listed_per_blocking_action_with_errors]
- AC5: output is redacted and replaced by a truncation marker above the cap [evidence: test:tests/test_hook_result_fields.py::test_output_capped_and_redacted]
- AC6: a decision the event does not honour is allow and recorded as invalid [evidence: test:tests/test_hook_result_fields.py::test_decision_not_honoured_by_event_is_allow]
- AC7: the hook_result event carries output [evidence: test:tests/test_hook_result_fields.py::test_hook_result_event_carries_output]
- AC8: non-blocking actions' annotations and output are ignored [evidence: test:tests/test_hook_result_fields.py::test_non_blocking_fields_ignored]
- AC9: existing dispatcher and router tests stay green [evidence: command:uv run pytest tests/test_hooks_command.py tests/test_hooks_webhook.py tests/test_router.py -q]

## Files

- `src/claude_code_wrapper/hooks.py`
- `src/claude_code_wrapper/router.py`
- `README.md`
- `tests/test_hook_result_fields.py`

## Interfaces

- Part `hook-result-fields`: `hooks.py::ActionResult` (`annotations`, `output`), `hooks.py::HookOutcome` (`annotations`, `outputs`), `hooks.py::EVENT_DECISIONS`, `hooks.py::OUTPUT_CAP_BYTES`, the decision parser. Consumers: stop-gate-onstop-event, turn-annotations-from-hooks, destroy-hook-input-output, the examples.

## Expected outcome

A blocking hook action can return `annotations` and `output` next to its decision; the outcome carries them merged and per action; the hook_result event shows the output; nothing changes for hooks that do not use them.

## Steps

- Write `tests/test_hook_result_fields.py` (command actions via small `sh -c` scripts, webhooks via `httpx.MockTransport`); see it fail.
- Extend the parser, `ActionResult`, `HookOutcome` and `fire`; add `EVENT_DECISIONS`; route output into `on_hook_result`.
- Update README hooks sections.
- `uv run pytest tests/test_hook_result_fields.py tests/test_hooks_command.py tests/test_hooks_webhook.py tests/test_router.py -q && mise run lint`.

## Notes

- Deny still only counts on `VETO_EVENTS` (config.py); keep that rule inside `EVENT_DECISIONS`, one place.
- Redact output with the dispatcher's `Redactor` before capping, so a secret never reaches events.jsonl.
