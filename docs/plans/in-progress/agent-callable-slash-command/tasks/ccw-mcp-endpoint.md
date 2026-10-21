---
title: ccw MCP endpoint
status: in-progress
repo: .
value: Claude can connect to ccw as an MCP server and call the one command tool
files: [src/claude_code_wrapper/mcp_server.py, src/claude_code_wrapper/internal_api.py, README.md, tests/test_mcp_server.py]
verify: "uv run pytest tests/test_mcp_server.py tests/test_internal_api.py tests/test_readme_api.py -q && mise run lint"
depends_on: [command-runner]
owns: [ccw-mcp-server]
uses: [command-request-api, command-queue, commands-config]
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

# ccw MCP endpoint

## Requirement
The ccw MCP server on the internal API (design D1; handshake probed on claude 2.1.304 and 2.1.306, design.md "Facts"). Owns part `ccw-mcp-server` (C/mcp_server.py).

- `POST /internal/mcp` on the internal app: loopback only (403 otherwise), `X-CCW-Session` plus `X-CCW-Hook-Token` checked with `manager.hook_token_matches` (constant time) else 403; the body is one JSON-RPC 2.0 message (an array -> error -32600; invalid JSON -> -32700); responses are `application/json`.
  - `initialize` -> `{protocolVersion, capabilities: {tools: {listChanged: false}}, serverInfo: {name: "ccw", version: <package version>}, instructions}`; protocolVersion echoes the client's when it is one of `2025-11-25`, `2025-06-18`, `2025-03-26`, else `2025-06-18`; `instructions` tell the agent that `command` queues a slash command which runs when it ends its turn, after which it is resumed automatically.
  - any message without an `id` (notifications) -> 202 with an empty body.
  - `ping` -> `{}`.
  - `tools/list` -> one tool `command`: a description, and `inputSchema` from `commands.params_schema(<the session's agent allowlist>)` (`name` an enum of that list, `params` optional, additionalProperties false). An empty allowlist lists no tools.
  - `tools/call` with name `command` -> `router.request_command(name, params, origin="agent")`; success returns `{content: [{type: "text", text: "Queued <id>. It runs when you end this turn; end your turn now with a one-line status. You will be resumed automatically."}], isError: false}`; a `CommandRejected` returns `isError: true` with its message (unknown params naming the allowed ones, not allowed, queue full, force not allowed, invalid state); an empty allowlist returns `isError: true` "ccw commands are disabled for this session"; another tool name -> JSON-RPC error -32602.
  - `server/discover` and every other method -> JSON-RPC error -32601 Method not found (claude then falls back to initialize, probed).
- `GET` and `DELETE /internal/mcp` -> 405.
- The session's allowlist is the router's `commands_cfg.agent` (per-session override included).
- README: `### POST /internal/mcp` (tests/test_readme_api.py requires every internal route), with the tool schema, the success text, the errors, and that GET and DELETE answer 405.

## Acceptance conditions

- AC1: /internal/mcp answers 403 without a matching session token or from a non-loopback client [evidence: test:tests/test_mcp_server.py::test_mcp_requires_session_token]
- AC2: initialize returns the negotiated protocol version, tools capability, serverInfo and instructions [evidence: test:tests/test_mcp_server.py::test_initialize_returns_instructions]
- AC3: server/discover and unknown methods get -32601, batches -32600 and bad JSON -32700 [evidence: test:tests/test_mcp_server.py::test_unknown_methods_and_bad_messages]
- AC4: a notification gets 202 with an empty body and ping returns an empty result [evidence: test:tests/test_mcp_server.py::test_notification_returns_202_and_ping]
- AC5: tools/list returns the one command tool with the session's agent allowlist as the name enum [evidence: test:tests/test_mcp_server.py::test_tools_list_enum_from_agent_allowlist]
- AC6: tools/call queues the command with origin agent and returns the queued text [evidence: test:tests/test_mcp_server.py::test_tools_call_queues_command]
- AC7: unknown params, a name outside the allowlist and a full queue come back as tool errors [evidence: test:tests/test_mcp_server.py::test_tools_call_rejections_are_tool_errors]
- AC8: an empty allowlist lists no tools and a call is a tool error [evidence: test:tests/test_mcp_server.py::test_disabled_session_has_no_tools]
- AC9: GET and DELETE /internal/mcp answer 405 [evidence: test:tests/test_mcp_server.py::test_get_and_delete_return_405]
- AC10: README documents the internal route and existing internal API tests stay green [evidence: command:uv run pytest tests/test_readme_api.py tests/test_internal_api.py -q]

## Files

- `src/claude_code_wrapper/mcp_server.py`
- `src/claude_code_wrapper/internal_api.py`
- `README.md`
- `tests/test_mcp_server.py`

## Interfaces

`mcp_server.handle(router, message: dict) -> tuple[int, dict | None]` (status, JSON body; None for 202) used by `internal_api`; route `POST /internal/mcp`.

## Expected outcome

`POST /internal/mcp` answers claude's handshake and exposes the one `command` tool for the session's agent allowlist; a tools/call queues the command with origin agent.

## Steps

- Write tests/test_mcp_server.py with httpx ASGITransport against create_internal_app and a World session; run it and see it fail.
- Add mcp_server.py and the route in internal_api.py; document the route in README.
- Run `uv run pytest tests/test_mcp_server.py tests/test_internal_api.py tests/test_readme_api.py -q && mise run lint`.
- A retirement test, if any, asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Reuse the hook route's auth and lookup (C/internal_api.py:55-63, `manager.router`); the tests call the internal app with httpx `ASGITransport` (client 127.0.0.1) against a World session, as tests/test_internal_api.py does.
