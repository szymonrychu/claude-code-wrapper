---
title: Slash epic review fixes
status: todo
repo: .
value: Closes a high-severity command-smuggling hole and a tool schema the Anthropic API may reject before the epic closes
files: [src/claude_code_wrapper/commands.py, src/claude_code_wrapper/mcp_server.py, src/claude_code_wrapper/data/config.example.yaml, README.md, tests/test_slash_review_fixes.py, tests/test_command_registry.py, tests/test_mcp_server.py]
verify: "uv run pytest tests/test_slash_review_fixes.py tests/test_command_registry.py tests/test_mcp_server.py tests/test_packaging.py -q && mise run lint"
depends_on: [slash-commands-docs, deflake-never-typed-while-bu]
owns: []
uses: []
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

# Slash epic review fixes

## Requirement

Epic-review remediation (whole-diff review, security review, scope audit, 2026-10-22). Fix all six, test-first:
1. HIGH commands.py validate_params: every string param (not only compact instructions) is rejected when it contains control characters (the existing INSTRUCTIONS_CONTROL set incl. CR/LF, U+2028/2029, C1) and is length-capped; `mcp` `server` must match ^[A-Za-z0-9_.:-]{1,64}$. A value smuggling a newline plus a second slash command, or `--force`, is rejected before anything is typed.
2. commands.py params_schema / the MCP tool input_schema: no top-level oneOf/anyOf/allOf (the Anthropic API rejects them for custom tools). Use a flat object: `name` (enum of the session's agent allowlist) and optional `params` (object whose properties are the union of per-command params, each with a description naming the command it applies to), additionalProperties false at both levels. Per-command validation stays in validate_params.
3. mcp_server.py tools/call: any argument key other than `name` and `params` returns an isError tool result naming the allowed keys (no silent drop, nothing queued).
4. README `command_*` event sections: the record fields are `id`, `name`, `args`/params, `origin`, `state`, `result`, `output`, `error`, timestamps (not `command_id`; `command_id` only in the onCommand hook payload).
5. README Slash commands section (and Trust model): document that main-agent-only is enforced by the PreToolUse relay on `mcp__ccw__command`; a subagent's Bash can call /internal/mcp directly with the session token in claude's env, which can only queue allowlisted commands (accepted under the trust model).
6. data/config.example.yaml: the commands.client comment must not contain the substring `/sessions/` (tests/test_packaging.py::test_example_config_has_no_hardcoded_container_paths); say "the public commands endpoint" instead.

## Acceptance conditions

- AC1: newline, control-character and over-long values in any string param are rejected, and a server name outside ^[A-Za-z0-9_.:-]{1,64}$ is rejected [evidence: test:tests/test_slash_review_fixes.py::test_string_params_reject_control_chars_and_bad_server]
- AC2: a smuggled second command or --force inside a param never reaches the PTY [evidence: test:tests/test_slash_review_fixes.py::test_param_smuggling_never_typed]
- AC3: the MCP tool input_schema has no top-level oneOf, anyOf or allOf and is a flat object with name and params [evidence: test:tests/test_slash_review_fixes.py::test_tool_schema_is_flat]
- AC4: tools/call with an unknown top-level argument returns isError and queues nothing [evidence: test:tests/test_slash_review_fixes.py::test_unknown_top_level_argument_rejected]
- AC5: README command event sections name the record field id, not command_id [evidence: command:! grep -n "command_id" README.md | grep -v -i "onCommand"]
- AC6: README documents the subagent direct-call limitation of main-agent-only [evidence: command:grep -qi "internal/mcp" README.md && grep -qi "subagent" README.md]
- AC7: lint and the full suite stay green, including tests/test_packaging.py [evidence: command:mise run lint && mise run test]

## Files

- `src/claude_code_wrapper/commands.py`
- `src/claude_code_wrapper/mcp_server.py`
- `src/claude_code_wrapper/data/config.example.yaml`
- `README.md`
- `tests/test_slash_review_fixes.py`
- `tests/test_command_registry.py`
- `tests/test_mcp_server.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
