---
title: Peer session bootstrap
status: todo
repo: .
value: "Peered agents cannot reach for Claude's native socket messaging and are told how to use the ccw tools"
files: [src/claude_code_wrapper/session.py, src/claude_code_wrapper/bootstrap/settings.py, src/claude_code_wrapper/bootstrap/instructions.py, src/claude_code_wrapper/router.py, tests/test_router.py, tests/test_peers_bootstrap.py, tests/test_peers_gate.py]
verify: "uv run pytest tests/test_peers_bootstrap.py tests/test_peers_gate.py tests/test_bootstrap_settings.py tests/test_bootstrap_home.py tests/test_router.py tests/test_session_boot.py -q && mise run lint"
depends_on: [peer-inbox-delivery, peers-config-section]
owns: [peer-session-bootstrap]
uses: [peers-config]
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

# Peer session bootstrap

## Requirement

Switch Claude's native peer messaging off and point the agent at the ccw tools, per session, only when peers are enabled (design D3, D4 instructions).

- src/claude_code_wrapper/session.py: `build_env(..., peers: bool = False)`; with `peers` the managed vars gain `CLAUDE_CODE_HARBOR_KITE=0`, so `claude.env.extra` or passthrough cannot override it. Default False keeps the env identical.
- src/claude_code_wrapper/bootstrap/settings.py: `render_settings(..., peers: bool = False)`; with `peers`, `permissions.deny` gains `SendMessage` and `ListAgents` (not duplicated when the user already lists them) and `permissions.allow` gains `mcp__ccw__peers` and `mcp__ccw__send_message` (constants `PEERS_ALLOW`); a re-render with peers off removes ccw-added allow entries the same way `CCW_MCP_ALLOW` is handled today. Disabled output is byte-identical.
- src/claude_code_wrapper/bootstrap/instructions.py: `write_instructions(config_dir, prompts_cfg, peers: bool = False)`; with `peers` a `PEERS_INSTRUCTIONS` paragraph is appended after the configured parts (and CLAUDE.md is written even when no other part exists): you can message the main agents of other ccw sessions; list them with `mcp__ccw__peers`; send with `mcp__ccw__send_message` to an address `<instance>/<session id>`; a message from a peer arrives as a turn framed in `<agent-message from="..." msg_id="...">`; reply with `mcp__ccw__send_message` to its `from` only when a reply is needed (no acknowledgements); `notify_when_idle: true` gets a `<peer-idle-notice>` when the peer has finished, and a `<peer-delivery-failed>` notice means the message was rejected or dropped; never use SendMessage or ListAgents. Disabled output is unchanged.
- src/claude_code_wrapper/router.py `_on_pre_tool_use`: when `cfg.peers.enabled` and the tool is `SendMessage` or `ListAgents`, deny with reason `use mcp__ccw__send_message / mcp__ccw__peers`, emit `tool_denied` with attribution and count `tool_call(tool, "deny")`, before the onAgentToolCall hook. `CCW_SUBAGENT_DENY_REASON` is reworded to name every ccw tool (`mcp__ccw__*`) instead of only `mcp__ccw__command`; the deny itself is unchanged and covers `mcp__ccw__send_message` and `mcp__ccw__peers`. The new text still contains `main agent only` (tests/test_command_registry.py and tests/test_mcp_injection.py match on it), and the exact-text pin in tests/test_router.py (around line 734) is updated to the new reason.
- tests/test_peers_bootstrap.py: env with and without peers (HARBOR_KITE=0 wins over a user value); settings deny and allow with peers, no duplicates, removal on re-render without peers, disabled byte-identical to a render made before the change (compare to `render_settings` with the default argument); CLAUDE.md paragraph with peers, alone and after configured parts, and unchanged without.
- tests/test_peers_gate.py with the router harness: SendMessage and ListAgents denied with the reason and a tool_denied event when enabled, allowed (reaching onAgentToolCall) when disabled; a subagent call to `mcp__ccw__send_message` denied with the reworded reason.

Dependency note: this task depends on peer-inbox-delivery only because both edit src/claude_code_wrapper/router.py (this task the PreToolUse gate, that one the inbox hooks), so the edits must not run in parallel. README text about the gate and the reworded reason is written by peers-image-compose-docs; this task does not edit README.md.

Interfaces: `build_env(peers=)`, `render_settings(peers=)`, `write_instructions(peers=)`, `PEERS_INSTRUCTIONS`, `PEERS_ALLOW` (the `peer-session-bootstrap` part).

## Acceptance conditions

- AC1: with peers the child env has CLAUDE_CODE_HARBOR_KITE=0 even over a user value, and without peers the env is unchanged [evidence: test:tests/test_peers_bootstrap.py::test_env_harbor_kite_off]
- AC2: with peers settings.json denies SendMessage and ListAgents and allows the two ccw peer tools without duplicates [evidence: test:tests/test_peers_bootstrap.py::test_settings_deny_and_allow]
- AC3: a re-render without peers removes the ccw-added peer allow entries and the disabled render is identical to the default render [evidence: test:tests/test_peers_bootstrap.py::test_settings_disabled_unchanged]
- AC4: with peers CLAUDE.md carries the peers paragraph, alone or after configured parts, and is unchanged without [evidence: test:tests/test_peers_bootstrap.py::test_instructions_paragraph]
- AC5: with peers enabled SendMessage and ListAgents are denied by the tool gate with the stated reason and a tool_denied event [evidence: test:tests/test_peers_gate.py::test_native_tools_denied_when_enabled]
- AC6: with peers disabled SendMessage reaches onAgentToolCall as before [evidence: test:tests/test_peers_gate.py::test_native_tools_untouched_when_disabled]
- AC7: a subagent call to mcp__ccw__send_message is denied with the reworded main-agent-only reason [evidence: test:tests/test_peers_gate.py::test_subagent_cannot_send]
- AC8: the existing bootstrap, env, gate and attribution suites stay green [evidence: command:uv run pytest tests/test_bootstrap_settings.py tests/test_bootstrap_home.py tests/test_session_boot.py tests/test_passthrough_globs.py tests/test_mcp_injection.py tests/test_router.py tests/test_agent_attribution.py tests/test_pretooluse_fail_closed.py -q]

## Files

- `src/claude_code_wrapper/session.py`
- `src/claude_code_wrapper/bootstrap/settings.py`
- `src/claude_code_wrapper/bootstrap/instructions.py`
- `src/claude_code_wrapper/router.py`
- `tests/test_router.py`
- `tests/test_peers_bootstrap.py`
- `tests/test_peers_gate.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
