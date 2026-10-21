---
title: ccw MCP injection and e2e
status: todo
repo: .
value: "The main agent can self-compact and self-reload plugins, with the token never stored in a file"
files: [src/claude_code_wrapper/bootstrap/claude_json.py, src/claude_code_wrapper/bootstrap/settings.py, src/claude_code_wrapper/manager.py, src/claude_code_wrapper/config.py, README.md, tests/test_mcp_injection.py]
verify: "uv run pytest tests/test_mcp_injection.py tests/test_bootstrap_home.py tests/test_bootstrap_settings.py tests/test_config.py tests/test_readme_config.py tests/test_manager.py -q && mise run lint"
depends_on: [ccw-mcp-endpoint, command-same-turn-resume, fake-claude-mcp-step]
owns: []
uses: [ccw-mcp-server, fake-claude-mcp, world-internal-forward, continue-turn, commands-config]
model: sonnet
estimate: 75
actual: ""
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/agent-callable-slash-command--ccw-mcp-injection-and-e2e
branch: agent-callable-slash-command--ccw-mcp-injection-and-e2e
verified_head: ""
run: agent-callable-slash-command
provides: []
acs_for_audit: []
process_gaps: []
---

# ccw MCP injection and e2e

## Requirement
Inject the ccw MCP server into each session's claude config and prove the agent path end to end with the fake (design D1, D2; goal "main agent can self-compact and self-reload plugins").

- `bootstrap/claude_json.py` `seed_claude_json(..., ccw_mcp: bool = False)`: when true, writes `mcpServers.ccw = {"type": "http", "url": "${CCW_INTERNAL_URL}/internal/mcp", "headers": {"X-CCW-Session": "${CCW_SESSION_ID}", "X-CCW-Hook-Token": "${CCW_HOOK_TOKEN}"}, "alwaysLoad": true}` exactly, so the token value is never written to a file (claude 2.1.304 and 2.1.306 expand these references, probed); when false, removes a `ccw` entry left by an earlier activation. Runs on create and on resume (both go through `_bootstrap`).
- `bootstrap/settings.py` `render_settings(..., ccw_mcp: bool = False)`: adds `mcp__ccw__command` to `permissions.allow` once when true; removes it when false.
- `manager.py`: `ccw_mcp` is true when the session's effective `commands.agent` list (config plus the create override) is non-empty; passes it to both.
- `config.py`: a `claude.mcp_servers` entry named `ccw` is a ConfigError at `claude.mcp_servers.ccw` ("the name ccw is reserved for the ccw command server") when `sessions.commands.agent` is non-empty; a create override that enables agent commands while such an entry exists is a 422 at create (my call: reserved name).
- End to end through the World (fake `mcp` step, part `world-internal-forward`), each asserting the event order and the single turn record:
  - self-compact: a turn calls `mcp__ccw__command {name: "compact", params: {instructions: "keep the plan"}}` then ends; ccw defers the Stop, types `/compact keep the plan`, the fake compacts, ccw pastes the continue prompt with the same marker, the fake's continuation turn answers, and the turn completes once: `command_queued`, `turn_deferred` (reason command), `command_started`, `compacted`, `command_completed`, `turn_resumed` (cause command), `turn_completed`;
  - self-reload: the same with `reload-plugins`, the record result `ok`, its output the fake's `Reloaded: ...` line;
  - subagent: an `mcp` step with `agent` gets the main-agent-only tool error, no command is queued and the turn completes normally;
  - disabled: with an empty agent list the fake's step gets "MCP server not configured" (no `ccw` entry).
- README: `### claude` notes the injected `ccw` server, its reserved name and the allow rule.

## Acceptance conditions

- AC1: claude.json gets the ccw server with env references and alwaysLoad when agent commands are enabled [evidence: test:tests/test_mcp_injection.py::test_claude_json_has_ccw_server_with_env_refs]
- AC2: the hook token value appears in no file under the session's config dir [evidence: test:tests/test_mcp_injection.py::test_token_never_written_to_disk]
- AC3: settings.json allows mcp__ccw__command once when enabled [evidence: test:tests/test_mcp_injection.py::test_settings_allow_ccw_command]
- AC4: disabling agent commands removes the ccw entry and the allow rule on resume [evidence: test:tests/test_mcp_injection.py::test_disabled_removes_ccw_server_and_allow]
- AC5: an operator mcp_servers entry named ccw is rejected while agent commands are enabled [evidence: test:tests/test_mcp_injection.py::test_reserved_ccw_server_name_rejected]
- AC6: the agent self-compacts through the tool and resumes in the same turn with the expected event order [evidence: test:tests/test_mcp_injection.py::test_agent_self_compact_same_turn_e2e]
- AC7: the agent self-reloads plugins through the tool and resumes in the same turn [evidence: test:tests/test_mcp_injection.py::test_agent_reload_plugins_e2e]
- AC8: a subagent's tool call is refused and queues nothing [evidence: test:tests/test_mcp_injection.py::test_subagent_call_refused_e2e]
- AC9: existing bootstrap, config and readme tests stay green [evidence: command:uv run pytest tests/test_bootstrap_home.py tests/test_bootstrap_settings.py tests/test_config.py tests/test_readme_config.py tests/test_manager.py -q]

## Files

- `src/claude_code_wrapper/bootstrap/claude_json.py`
- `src/claude_code_wrapper/bootstrap/settings.py`
- `src/claude_code_wrapper/manager.py`
- `src/claude_code_wrapper/config.py`
- `README.md`
- `tests/test_mcp_injection.py`

## Interfaces

`seed_claude_json(..., ccw_mcp=False)`, `render_settings(..., ccw_mcp=False)`.

## Expected outcome

Sessions with agent commands get the ccw server and allow rule in their own claude config with env references only, and the fake agent self-compacts and self-reloads in the same turn through it.

## Steps

- Write tests/test_mcp_injection.py (bootstrap unit tests and World e2e tests with the fake's `mcp` step); run it and see it fail.
- Add the ccw_mcp parameter to seed_claude_json and render_settings, the manager wiring, the reserved-name validator and the README note.
- Run `uv run pytest tests/test_mcp_injection.py tests/test_bootstrap_home.py tests/test_bootstrap_settings.py tests/test_config.py tests/test_readme_config.py tests/test_manager.py -q && mise run lint`.
- A retirement test, if any, asserts on `git ls-files <path>`, never on the filesystem.

## Notes

`CCW_INTERNAL_URL`, `CCW_SESSION_ID` and `CCW_HOOK_TOKEN` are already in claude's env (C/session.py:244-245). The fake's `fake-mcp.jsonl` shows the expanded headers it sent; the token-never-on-disk test greps the session's config dir for the token value.

- 2026-10-21 run agent-callable-slash-command: dependency-failed on ccw-mcp-endpoint, command-same-turn-resume

- 2026-10-21 run agent-callable-slash-command: dependency-failed on command-same-turn-resume
