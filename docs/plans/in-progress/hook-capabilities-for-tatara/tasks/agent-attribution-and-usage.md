---
title: Agent attribution and usage
status: in-progress
repo: .
value: "Tool events and usage say which agent acted, so per-role activity and tokens are derivable (K5)"
files: [src/claude_code_wrapper/router.py, src/claude_code_wrapper/events.py, README.md, tests/test_agent_attribution.py]
verify: "uv run pytest tests/test_agent_attribution.py tests/test_router.py tests/test_readme_api.py -q && mise run lint"
depends_on: [turn-annotations-from-hooks]
owns: [agent-attribution]
uses: [fake-claude-scenarios, ccw-world-fixture]
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

# Agent attribution and usage

## Requirement

K5 agent attribution and per-message usage events (design D9). Owns part `agent-attribution`.

- `tool_call`, `tool_result` (and `tool_denied`) events and the `onAgentToolCall`/`onToolResult` hook data gain `agent_id` and `agent_type` from the Claude hook payload (null for the main agent).
- A new `usage` event per assistant message, emitted once per `message_id` (the same dedupe rule as `UsageTotals`): `{message_id, model, agent_id, input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens}`, for main-chain and sidechain messages. No `usage` events while a resumed transcript's history is being dropped.
- `usage` joins `EVENT_TYPES`.
- README: `### \`usage\`` under `## Events (SSE)` and the new fields on `tool_call` and `tool_result`; `onAgentToolCall` and `onToolResult` data lists.

## Acceptance conditions

- AC1: tool_call and tool_result carry agent_id and agent_type for a subagent tool call [evidence: test:tests/test_agent_attribution.py::test_tool_events_carry_agent_attribution]
- AC2: onAgentToolCall and onToolResult data carry agent_id and agent_type [evidence: test:tests/test_agent_attribution.py::test_tool_hooks_receive_agent_attribution]
- AC3: main-agent tool events have null agent fields [evidence: test:tests/test_agent_attribution.py::test_main_agent_tool_events_have_null_agent]
- AC4: one usage event per assistant message id [evidence: test:tests/test_agent_attribution.py::test_usage_event_per_message_once]
- AC5: sidechain usage events carry the agent_id [evidence: test:tests/test_agent_attribution.py::test_sidechain_usage_event_has_agent_id]
- AC6: dropped resumed history emits no usage events [evidence: test:tests/test_agent_attribution.py::test_resumed_history_emits_no_usage]
- AC7: README documents the usage event and the new fields [evidence: command:uv run pytest tests/test_readme_api.py -q]

## Files

- `src/claude_code_wrapper/router.py`
- `src/claude_code_wrapper/events.py`
- `README.md`
- `tests/test_agent_attribution.py`

## Interfaces

- Part `agent-attribution`: `agent_id`, `agent_type` in `tool_call`, `tool_result`, `tool_denied` event data and in onAgentToolCall/onToolResult data; event type `usage`.

## Expected outcome

Consumers can attribute tool calls and token usage to the main agent or a specific subagent or teammate from the event stream alone.

## Steps

- Write `tests/test_agent_attribution.py` with the fake's tool `agent` step and a subagent step; see it fail.
- Add the fields in `_on_pre_tool_use`/`_on_post_tool_use`; emit `usage` from `_on_transcript_events` using the dedupe of `UsageTotals.add`.
- README.
- `uv run pytest tests/test_agent_attribution.py tests/test_router.py tests/test_readme_api.py -q && mise run lint`.

## Notes

- Upstream: agent_id/agent_type are in Pre/PostToolUse inside subagents (2.1.69) and teammates (2.1.290).
- `UsageTotals.add` returns whether the message was new: use it to emit once.
