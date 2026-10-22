---
title: Peers MCP tool
status: todo
repo: .
value: The main agent can ask ccw which peers are live and get their uds addresses for SendMessage
files: [src/claude_code_wrapper/mcp_server.py, src/claude_code_wrapper/internal_api.py, src/claude_code_wrapper/manager.py, README.md, tests/test_mcp_peers.py, tests/test_mcp_server.py]
verify: "uv run pytest tests/test_mcp_peers.py tests/test_mcp_server.py tests/test_internal_api.py tests/test_readme_api.py -q && mise run lint"
depends_on: [peer-directory-module]
owns: [ccw-mcp-peers-tool]
uses: [peer-directory, peers-dir-fixture]
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

# Peers MCP tool

## Requirement

The ccw MCP server (src/claude_code_wrapper/mcp_server.py, today a single `command` tool) gets a second tool `peers`, registered only when peer messaging is enabled (design D6). Owns part `ccw-mcp-peers-tool` (cross-epic contract `ccw-mcp-peers-tool`); extends the `ccw-mcp-tool` contract owned by the done epic agent-callable-slash-command without changing the `command` tool.

- `handle_message` becomes `async def handle_message(router, message, peers: PeerDirectory | None = None)`; internal_api.py `/internal/mcp` awaits it and passes `manager.peers`. Everything about the `command` tool (schema, texts, errors, notifications, ping, -32601/-32600/-32700) is unchanged.
- `SessionManager.__init__` (manager.py) gains the attribute `self.peers: PeerDirectory | None = None` (only the attribute; manager-peers-lifecycle builds it), so the internal app can read it on every manager.
- `initialize`: `instructions` is today's `INSTRUCTIONS` when `peers` is None; when `peers` is given it is `PEERS_INSTRUCTIONS` (one sentence: the `peers` tool lists other live Claude sessions with their `uds:` addresses; message one with SendMessage `to` set to that address), prefixed by `INSTRUCTIONS` and a space when the agent allowlist is non-empty.
- `tools/list`: the `command` tool exactly as today (only with a non-empty allowlist), followed by `{"name": "peers", "description": PEERS_DESCRIPTION, "inputSchema": {"type": "object", "properties": {"instance": {"type": "string", "description": "only peers of this ccw instance"}}, "additionalProperties": false}}` when `peers` is not None.
- `tools/call` name `peers`:
  - `peers` None -> JSON-RPC error -32602 "unknown tool" (as for any unknown name today).
  - `arguments` missing or `{}` is fine; not an object -> tool error; unknown keys -> tool error `unknown arguments <keys>; allowed: instance`; a non-string `instance` -> tool error.
  - Success: `await peers.live_peers(exclude=router.info.session_id, instance=<instance or None>)`; result text is compact JSON `{"peers": [...], "hint": "..."}` where each peer is its directory entry plus `"hint": "SendMessage with to=\"<address>\""`, and the top-level hint says to use SendMessage with a peer's `address` (or that no live peers were found when the list is empty); `isError: false`.
- README `### POST /internal/mcp` documents the `peers` tool: when it is listed, its input, the result shape and the caller exclusion.
- tests/test_mcp_peers.py drives the internal app as tests/test_mcp_server.py does (httpx ASGITransport, client 127.0.0.1) with a FakeManager whose `peers` is a real `PeerDirectory` on the `peers_dir` fixture, live peers being real AF_UNIX listeners. tests/test_mcp_server.py's FakeManager gains `peers = None` and its tests stay green unchanged otherwise.

Interfaces: `async mcp_server.handle_message(router, message, peers=None) -> dict | None`; tool `peers` input `{instance?: string}`, output text JSON `{peers: [{session_id, name, instance, address, cwd, started_at, hint}], hint}`; `SessionManager.peers`.

Notes: reuse `_tool_result` and `error_response`; `live_peers` is async (concurrent probes), which is why handle_message becomes async rather than blocking the event loop on socket connects.

## Acceptance conditions

- AC1: without peers, initialize, tools/list and tools/call answer exactly as before [evidence: test:tests/test_mcp_peers.py::test_no_peers_tool_when_disabled]
- AC2: with peers, tools/list ends with the peers tool and its instance-only schema, also with an empty agent allowlist [evidence: test:tests/test_mcp_peers.py::test_tools_list_includes_peers]
- AC3: with peers, initialize instructions mention the peers tool and SendMessage [evidence: test:tests/test_mcp_peers.py::test_initialize_instructions_with_peers]
- AC4: tools/call peers returns live peers excluding the caller, each with its uds address and a SendMessage hint [evidence: test:tests/test_mcp_peers.py::test_peers_call_lists_live_peers]
- AC5: the instance argument filters peers, and bad or unknown arguments are tool errors [evidence: test:tests/test_mcp_peers.py::test_peers_call_arguments]
- AC6: an empty directory returns an empty list with a no-live-peers hint [evidence: test:tests/test_mcp_peers.py::test_peers_call_empty]
- AC7: the existing MCP server and internal API suites stay green [evidence: command:uv run pytest tests/test_mcp_server.py tests/test_internal_api.py tests/test_readme_api.py tests/test_mcp_injection.py -q]

## Files

- `src/claude_code_wrapper/mcp_server.py`
- `src/claude_code_wrapper/internal_api.py`
- `src/claude_code_wrapper/manager.py`
- `README.md`
- `tests/test_mcp_peers.py`
- `tests/test_mcp_server.py`

## Interfaces

As in the Requirement's Interfaces paragraph: async `mcp_server.handle_message(router, message, peers=None)`, tool `peers` (contract `ccw-mcp-peers-tool`), `SessionManager.peers`; the `command` tool (contract `ccw-mcp-tool`) is unchanged.

## Expected outcome

A peers-enabled session's ccw MCP server lists a `peers` tool that returns live peers with uds addresses and SendMessage hints; with peers off the server answers exactly as before.

## Steps

- Write the tests named in the acceptance conditions in `tests/test_mcp_peers.py` first; run `uv run pytest tests/test_mcp_peers.py -q` and see them fail for the missing behaviour (not an import typo).
- Implement the smallest change that makes them pass; run `uv run pytest tests/test_mcp_peers.py -q` and expect all passed.
- Run the task verify: `uv run pytest tests/test_mcp_peers.py tests/test_mcp_server.py tests/test_internal_api.py tests/test_readme_api.py -q && mise run lint`; expect pytest all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

See the Requirement's Notes paragraph.
- Builder check: the manager.py change in this task is only the `self.peers: PeerDirectory | None = None` attribute (Requirement bullet on SessionManager.__init__).
