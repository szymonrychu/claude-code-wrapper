---
title: Fake claude MCP step
status: todo
repo: .
value: "The agent's mcp__ccw__command call is testable end to end through the fake and the World"
files: [tests/fake_claude/fake_claude.py, tests/conftest.py, tests/test_fake_claude_mcp.py]
verify: "uv run pytest tests/test_fake_claude_mcp.py tests/test_fake_claude.py tests/test_fake_claude_commands.py -q && mise run lint"
depends_on: [fake-claude-slash-commands]
owns: [fake-claude-mcp, world-internal-forward]
uses: [fake-claude-commands]
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

# Fake claude MCP step

## Requirement
Give the fake claude an MCP tool step that talks HTTP JSON-RPC to the server configured in its claude.json, and let the test World forward the internal API, so the agent-side `mcp__ccw__command` path is testable end to end (design D1, D2; probe results in design.md "Facts"). Owns parts `fake-claude-mcp` and `world-internal-forward`. This is the only task in the epic that edits `tests/conftest.py`.

- New turn step `{"mcp": {"server": "ccw", "tool": "command", "arguments": {...}, "agent": {"id": A, "type": T}}}` (`agent` optional):
  - reads `$CLAUDE_CONFIG_DIR/.claude.json` `mcpServers[server]`; a missing entry gives a tool_result error `MCP server "<server>" is not configured` and no HTTP request;
  - expands `${VAR}` in `url` and in every header value from the process environment (unset expands to empty), as claude 2.1.304 and 2.1.306 do (probed);
  - fires PreToolUse with `tool_name` `mcp__<server>__<tool>`, `tool_input` = arguments, and `agent_id`/`agent_type` when `agent` is given; a deny ends the step with a tool_result error carrying the deny reason and sends no HTTP request;
  - otherwise POSTs, with the expanded headers plus `Content-Type: application/json` and `Accept: application/json, text/event-stream`: `server/discover` (a JSON-RPC error answer is accepted and ignored, as real claude does), `initialize` (protocolVersion `2025-11-25`), the `notifications/initialized` notification, then `tools/call {name: tool, arguments}`;
  - writes the tool_use and tool_result transcript lines (to the subagent file when `agent` is given, as the existing `tool` step does); the result text is the concatenated `text` content items; `isError` true from `result.isError` or a JSON-RPC `error`; then PostToolUse, or PostToolUseFailure on error;
  - appends `{url, headers, requests, responses}` to `$CLAUDE_CONFIG_DIR/fake-mcp.jsonl` so tests can assert what was sent (header values included, which is how a test proves env expansion happened in the fake and the token did not come from a file).
- `tests/conftest.py` World: its loopback server forwards every request under `/internal/` except `/internal/hooks/*` (methods POST, GET, DELETE) to `claude_code_wrapper.internal_api.create_internal_app(manager)` of the manager owning the request's `X-CCW-Session` header, through `httpx.ASGITransport` with a client address of 127.0.0.1, one app cached per manager; method, path, query, headers and body pass through; the internal app's status, headers and body come back unchanged. No manager for the header: 403. The existing hook route and every existing fixture keep their behaviour.
- Fake module docstring documents the step and `fake-mcp.jsonl`.

## Acceptance conditions

- AC1: the mcp step expands ${VAR} in url and headers and sends server/discover, initialize, initialized and tools/call [evidence: test:tests/test_fake_claude_mcp.py::test_mcp_step_expands_env_and_calls_tool]
- AC2: the tool result text and isError reach the transcript and PostToolUse or PostToolUseFailure [evidence: test:tests/test_fake_claude_mcp.py::test_mcp_step_result_and_error_reach_transcript]
- AC3: a PreToolUse deny on the mcp step sends no HTTP request and records the reason as a tool error [evidence: test:tests/test_fake_claude_mcp.py::test_mcp_step_denied_skips_http]
- AC4: an mcp step with agent carries agent_id on PreToolUse and writes to the subagent transcript [evidence: test:tests/test_fake_claude_mcp.py::test_mcp_step_agent_attribution]
- AC5: a missing mcpServers entry gives a tool error without HTTP [evidence: test:tests/test_fake_claude_mcp.py::test_mcp_step_missing_server]
- AC6: the World forwards /internal/healthz for a live session to the internal app and answers 403 for an unknown session [evidence: test:tests/test_fake_claude_mcp.py::test_world_forwards_internal_routes]
- AC7: the existing fake claude and session tests stay green [evidence: command:uv run pytest tests/test_fake_claude.py tests/test_fake_claude_commands.py tests/test_session_turns.py -q]

## Files

- `tests/fake_claude/fake_claude.py`
- `tests/conftest.py`
- `tests/test_fake_claude_mcp.py`

## Interfaces

part `fake-claude-mcp` = the `mcp` step and `fake-mcp.jsonl`; part `world-internal-forward` = World forwarding `/internal/*` (not hooks) to `create_internal_app(manager)`.

## Expected outcome

A scenario step can make the fake call `mcp__ccw__command` over HTTP exactly as claude does (env-expanded url and headers, server/discover then initialize then tools/call), and the World routes such calls to the real internal app of the right manager.

## Steps

- Write tests/test_fake_claude_mcp.py: a stub JSON-RPC server inside the test for the step tests, and a World session plus GET /internal/healthz through the World for the forward test; run it and see it fail.
- Implement the `mcp` step in tests/fake_claude/fake_claude.py and the `/internal/*` forward in tests/conftest.py (httpx.ASGITransport, one cached app per manager).
- Run `uv run pytest tests/test_fake_claude_mcp.py tests/test_fake_claude.py tests/test_fake_claude_commands.py -q && mise run lint`; expect all passed.
- A retirement test, if any, asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Until task ccw-mcp-endpoint adds `/internal/mcp`, the forwarded request gets the internal app's 404; the tests here use a stub JSON-RPC server inside the test for the step, and `/internal/healthz` through the World for the forward.
