---
title: Peers MCP tools
status: todo
repo: .
value: The main agent can list peers and send them messages through two ccw tools
files: [src/claude_code_wrapper/mcp_server.py, src/claude_code_wrapper/internal_api.py, tests/test_mcp_peers.py]
verify: "uv run pytest tests/test_mcp_peers.py tests/test_mcp_server.py tests/test_internal_api.py tests/test_fake_claude_mcp.py -q && mise run lint"
depends_on: [peer-http-client, peer-directory-module]
owns: [ccw-mcp-peers-tool]
uses: [peer-wire, peer-directory-entry]
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

# Peers MCP tools

## Requirement

The ccw MCP tools `peers` and `send_message` (design D4).

- src/claude_code_wrapper/mcp_server.py:
  - `class PeerTools(Protocol)`: `async list_peers(caller_session: str, instance: str | None) -> list[dict]` (each `{address, instance, session_id, name, cwd}`) and `async send(caller_session: str, to: str, message: str, notify_when_idle: bool) -> str` returning the msg_id or raising `peer_client.PeerError`.
  - `async def handle_message(router, message, peers: PeerTools | None = None)`. With `peers` None every existing behaviour (initialize, ping, tools/list, the `command` tool, errors) is byte-identical to today.
  - With `peers`, `tools/list` adds `peers` (input schema: optional string `instance`, no additional properties) and `send_message` (required string `to`, required string `message`, optional boolean `notify_when_idle`, no additional properties) after `command` (which is still listed only when the agent allowlist is non-empty); `initialize` instructions append a peers sentence.
  - `peers` call: JSON text of `await list_peers(router.info.session_id, instance)`; a non-string `instance` is an `invalid arguments` error result.
  - `send_message` call: unknown keys, a non-string `to` or `message`, an empty message or a non-boolean `notify_when_idle` give an error result starting `invalid arguments`; otherwise `await peers.send(...)`; success text `Sent <msg_id> to <to>. A reply, if any, arrives as a new turn.`; a `PeerError` becomes an error result with its message (which starts with the D4 prefix).
  - Calling `peers` or `send_message` with `peers` None is the existing `unknown tool` error.
- src/claude_code_wrapper/internal_api.py: `/internal/mcp` awaits `handle_message(router, message, peers=getattr(manager, "peers", None))`.
- The tool descriptions name the error prefixes `unknown peer`, `peer unreachable`, `message too large`, `inbox full`, `rate limited`, `peer rejected the request`, `invalid arguments` (design D4). README documentation of the tools is done by peers-image-compose-docs; this task does not edit README.md.
- tests/test_mcp_peers.py with a fake `PeerTools`: both tools listed only with peers; schemas as stated; peers listing passes the caller and filter and returns JSON; send success text; each PeerError code (including rate_limited) surfaces as an error result with its prefix; argument validation errors; unknown tool without peers; the `command` tool unchanged with and without peers.

Interfaces: `mcp_server.PeerTools`, `mcp_server.handle_message(router, message, peers=None)` now async (the `ccw-mcp-peers-tool` part; consumer change to the cross-epic `ccw-mcp-tool` contract).

Notes: only internal_api.py calls `handle_message` today (tests reach it over HTTP), so the async change touches no existing test; their expectations must not change.

## Acceptance conditions

- AC1: peers and send_message are listed with the stated schemas only when peers tools are given [evidence: test:tests/test_mcp_peers.py::test_tools_listed_only_with_peers]
- AC2: the peers tool returns the caller-excluded listing as JSON and passes the instance filter [evidence: test:tests/test_mcp_peers.py::test_peers_tool_lists]
- AC3: send_message returns the sent text with the msg_id on success [evidence: test:tests/test_mcp_peers.py::test_send_message_success]
- AC4: every PeerError code surfaces as an error result starting with its D4 prefix [evidence: test:tests/test_mcp_peers.py::test_send_message_errors]
- AC5: bad arguments give an invalid arguments error result without calling send [evidence: test:tests/test_mcp_peers.py::test_send_message_argument_validation]
- AC6: without peers the two tools are unknown and the command tool behaves exactly as before [evidence: test:tests/test_mcp_peers.py::test_command_tool_unchanged]
- AC7: the existing MCP server, internal API and injection suites stay green [evidence: command:uv run pytest tests/test_mcp_server.py tests/test_internal_api.py tests/test_mcp_injection.py tests/test_fake_claude_mcp.py -q]

## Files

- `src/claude_code_wrapper/mcp_server.py`
- `src/claude_code_wrapper/internal_api.py`
- `tests/test_mcp_peers.py`

## Interfaces

See the Requirement's Interfaces paragraph; keep every name and signature stated there.

## Expected outcome

The ccw MCP server offers peers and send_message when given PeerTools, with exact schemas and error texts, and is unchanged otherwise.

## Steps

- Write the tests named in the acceptance conditions in `tests/test_mcp_peers.py` first; run `uv run pytest tests/test_mcp_peers.py -q` and see them fail for the missing behaviour (not an import typo).
- Implement the smallest change that makes them pass; run `uv run pytest tests/test_mcp_peers.py -q` again and expect all passed.
- Run the task verify: `uv run pytest tests/test_mcp_peers.py tests/test_mcp_server.py tests/test_internal_api.py tests/test_fake_claude_mcp.py -q && mise run lint`; expect all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

See the Requirement's Notes paragraph. internal_api reads manager.peers, which manager-peers-lifecycle sets; getattr keeps it None meanwhile.
