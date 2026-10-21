---
title: Command registry and config
status: done
repo: .
value: "Operators decide which slash commands agents and clients may run, per service and per session"
files: [src/claude_code_wrapper/config.py, src/claude_code_wrapper/commands.py, src/claude_code_wrapper/manager.py, src/claude_code_wrapper/router.py, README.md, tests/test_command_registry.py]
verify: "uv run pytest tests/test_command_registry.py tests/test_config.py tests/test_manager.py tests/test_router.py tests/test_api_sessions.py tests/test_readme_config.py tests/test_readme_api.py -q && mise run lint"
depends_on: []
owns: [command-queue, commands-config]
uses: []
model: sonnet
estimate: 90
actual: ""
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/agent-callable-slash-command--command-registry-and-config
branch: agent-callable-slash-command--command-registry-and-config
verified_head: 5aee6a34e596fc6d28dabc608143e4cb8ba90d7a
run: agent-callable-slash-command
provides: [commands-override-model, command-validation-render-schema]
acs_for_audit: []
process_gaps: ["The implementer states the code was written before the tests, so there is no red run, which breaks the test-first rule in the project CLAUDE.md. No exception applies, since this is a behaviour change."]
---

# Command registry and config

## Requirement
Command registry, per-session queue, config and the main-agent guard (design D3, D2, D1 params). Owns parts `command-queue` (C/commands.py) and `commands-config` (C/config.py). Nothing here runs a command; task command-runner does.

- `config.py`: `CommandsConfig` at `sessions.commands` (extra forbid): `agent: list[str] = []`, `client: list[str] = []`, `timeout_seconds: dict[str, float] = {"compact": 600, "default": 60}` (keys: a built-in name or `default`; values > 0), `continue_prompt: str` (non-empty default telling the agent its queued commands ran and to continue the task), `allow_force: bool = False`, `quiet_seconds: float = 1.5` (>= 0). Any name outside the built-in set in `agent`, `client` or `timeout_seconds` keys is a ConfigError at its path, naming the allowed set, for example `sessions.commands.agent.0: unknown command 'clear'; allowed: compact, context, mcp, reload-plugins, reload-skills`. With nothing configured both lists are empty and behaviour is unchanged.
- `commands.py` (new):
  - `BUILTIN_COMMANDS`: compact (params `instructions?: str`), reload-plugins (`force?: bool`), reload-skills (none), mcp (`action: "reconnect"` required, `server?: str`, default all), context (none).
  - `validate_params(name, params, *, allow_force) -> dict` raising `CommandRejected(code, message)`: unknown param names -> code `invalid_params` with a message naming the allowed ones (or "takes no params"); wrong types -> `invalid_params`; `force: true` without allow_force -> `force_not_allowed`; compact instructions must be one line, no control characters, at most 2000 characters (they are typed into the PTY) -> `invalid_params`.
  - `render(name, params) -> str`: `/compact`, `/compact <instructions>`, `/reload-plugins`, `/reload-plugins --force`, `/reload-skills`, `/mcp reconnect <server or all>`, `/context`.
  - `params_schema(names) -> dict`: the JSON schema of the MCP tool input `{name: enum of names, params?: object}` documenting each command's params (oneOf per name or a described object), used by task ccw-mcp-endpoint.
  - `CommandRecord` (pydantic): `id` (`cmd_<hex>`), `name`, `args` (the validated params), `origin` (`agent`|`client`|`idle`), `turn_id` (str or None), `state` (`queued`|`running`|`completed`|`failed`|`cancelled`), `result` (str or None, for example `ok`, `skipped_cache`), `error` (code or None), `output` (str or None), `created_at`, `started_at`, `finished_at`.
  - `CommandQueue(cfg: CommandsConfig)`: `request(name, params, *, origin, turn_id) -> tuple[CommandRecord, bool]` (bool: newly created); origin `agent` checks `cfg.agent`, `client` checks `cfg.client`, `idle` allows `compact` only; not allowed -> `CommandRejected("not_allowed")`; a name already queued returns that record with False (compact keeps the first instructions); more than 4 queued -> `CommandRejected("queue_full")`; `pending()`, `next()`, `get(id)`, `list()` (the last 100 kept, oldest first), `start(record)`, `finish(record, state, result=None, error=None, output=None)`, `cancel_queued(turn_id=None)`; timestamps set on each transition; a terminal record never changes again.
  - `effective_commands(base: CommandsConfig, override) -> CommandsConfig` merges a per-session override field by field.
- `manager.py`: `CreateSessionRequest.commands` (optional, same fields all optional, extra forbid, same unknown-name rule) is stored in the session meta and reapplied on resume; the manager passes `effective_commands(cfg.sessions.commands, meta override)` to the router.
- `router.py`: `SessionRouter.__init__(..., commands: CommandsConfig | None = None)` keeps `self.commands_cfg` (the config default when None) and `self.commands = CommandQueue(self.commands_cfg)`. D2: in `_on_pre_tool_use`, a `tool_name` starting with `mcp__ccw__` whose payload has a non-empty `agent_id` is denied before any user hook, with reason `ccw commands are available to the main agent only; subagents and teammates cannot call mcp__ccw__command.` and a `tool_denied` event carrying the attribution; the main agent's call (no agent_id) goes on as today.
- README: a config row for every `sessions.commands.*` key (tests/test_readme_config.py) and the `commands` field of the `POST /v1/sessions` body.

## Acceptance conditions

- AC1: sessions.commands defaults to empty lists and the documented timeouts, quiet_seconds and allow_force [evidence: test:tests/test_command_registry.py::test_commands_config_defaults_off]
- AC2: an unknown command name in agent, client or timeout_seconds is a config error naming the allowed set [evidence: test:tests/test_command_registry.py::test_unknown_command_name_rejected_at_config]
- AC3: unknown params are rejected with a message naming the allowed ones [evidence: test:tests/test_command_registry.py::test_unknown_param_rejected_naming_allowed]
- AC4: force is rejected unless allow_force, and multi-line or oversized compact instructions are rejected [evidence: test:tests/test_command_registry.py::test_force_and_instruction_limits]
- AC5: render produces the exact slash-command lines [evidence: test:tests/test_command_registry.py::test_render_command_lines]
- AC6: the queue enforces the agent and client allowlists and lets idle queue compact only [evidence: test:tests/test_command_registry.py::test_queue_allowlist_per_origin]
- AC7: a duplicate queued name returns the existing record and keeps the first instructions [evidence: test:tests/test_command_registry.py::test_duplicate_queued_returns_existing]
- AC8: a fifth queued command is rejected as queue_full and terminal records do not change [evidence: test:tests/test_command_registry.py::test_queue_cap_and_terminal_records]
- AC9: params_schema enumerates the given names and documents each command's params [evidence: test:tests/test_command_registry.py::test_params_schema_lists_allowed_commands]
- AC10: a per-session commands override on create is persisted and reapplied on resume [evidence: test:tests/test_command_registry.py::test_create_override_persisted_and_resumed]
- AC11: a subagent's mcp__ccw__ PreToolUse is denied before user hooks with a tool_denied event, the main agent's is not [evidence: test:tests/test_command_registry.py::test_subagent_ccw_tool_call_refused]
- AC12: README documents every new config key and the create body field [evidence: command:uv run pytest tests/test_readme_config.py tests/test_readme_api.py -q]
- AC13: existing config, manager and router tests stay green [evidence: command:uv run pytest tests/test_config.py tests/test_manager.py tests/test_router.py tests/test_api_sessions.py -q]

## Files

- `src/claude_code_wrapper/config.py`
- `src/claude_code_wrapper/commands.py`
- `src/claude_code_wrapper/manager.py`
- `src/claude_code_wrapper/router.py`
- `README.md`
- `tests/test_command_registry.py`

## Interfaces

`commands.BUILTIN_COMMANDS`, `commands.CommandRejected(code, message)`, `commands.validate_params`, `commands.render`, `commands.params_schema`, `commands.CommandRecord`, `commands.CommandQueue`, `commands.effective_commands`; `config.CommandsConfig` at `sessions.commands`; `SessionRouter(commands=...)`, `SessionRouter.commands`, `SessionRouter.commands_cfg`.

## Expected outcome

`sessions.commands` validates against the built-in command set, each session has a CommandQueue enforcing allowlists, coalescing and the cap, a create request can override the lists per session, and a subagent's ccw tool call is denied.

## Steps

- Write tests/test_command_registry.py (pure unit tests for commands.py and config, World tests for the override and the PreToolUse refusal); run it and see it fail.
- Add CommandsConfig to config.py, the new commands.py, the CreateSessionRequest field and meta plumbing in manager.py, the router kwarg and the PreToolUse refusal in router.py, the README rows.
- Run `uv run pytest tests/test_command_registry.py tests/test_config.py tests/test_manager.py tests/test_router.py tests/test_api_sessions.py tests/test_readme_config.py tests/test_readme_api.py -q && mise run lint`.
- A retirement test, if any, asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Design D-numbers and facts with file:line are in ../design.md.

- 2026-10-21 hand-verified on 5aee6a3 after verify-agent-died: verify-task.py --base 28c5cfc --tdd-revert passed, AC1-AC13 met, tdd failedOnBase/passedOnHead, clean.
