---
title: Config models
status: todo
repo: claude-code-wrapper
value: One validated config.yaml drives the whole service
files: [src/claude_code_wrapper/config.py, src/claude_code_wrapper/interpolation.py, tests/test_config.py, tests/test_interpolation.py, config.example.yaml]
verify: "uv run pytest tests/test_config.py tests/test_interpolation.py -q && mise run lint"
depends_on: [claude-env-model]
owns: [config-schema]
uses: [claude-env]
model: ""
estimate: 90
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Config models

## Requirement

Build the config.yaml models, loader and interpolation (D8), exactly per design.md "Config schema", "Hook events" and the hook action shape.

`src/claude_code_wrapper/interpolation.py`: `interpolate(obj, env: Mapping[str, str], path="") -> obj` walks dicts and lists and rewrites string scalars only: `${VAR}` (unset raises `InterpolationError` naming the dotted YAML path and the var), `${VAR:-default}` (default when unset or empty), `$$` -> literal `$`. Keys are never interpolated. Non-string scalars untouched.

`src/claude_code_wrapper/config.py`:
- Models `Config`, `ServerConfig`, `PathsConfig`, `ClaudeConfig` (with `env: ClaudeEnv`, `passthrough_env`, `permissions`, `settings`, `claude_json`, `mcp_servers` with `McpServer` stdio/http/sse variants, `config_files` [{source, target}], `boot` with dialogs/ready_markers/fatal_patterns defaults from design.md), `InitConfig` (`workdir` mode empty|mounted|clone with path, name, repo_url, branch, depth, submodules; `git`; `setup_commands` accepting strings or `{run, timeout_seconds}`), `PromptsConfig` (`global_claude_md`, `system_append`, `initial_prompt` str or `{template, variables}`, `templates`), `MiseConfig`, `PluginsConfig` (marketplaces [{name, source}], install [{plugin, scope, required}], disable, timeout_seconds), `HooksConfig` (one optional list per event name), `SessionsConfig`, `LimitsConfig`; every model `extra="forbid"`; defaults as in design.md.
- Exports `HOOK_EVENTS` (the 12 names in design order: onCreate, onResume, onDestroy, onTurn, onTurnComplete, onAgentResponse, onAgentToolCall, onToolResult, onSubagentStart, onSubagentStop, onInterrupt, onError), `VETO_EVENTS = (onCreate, onResume, onTurn, onAgentToolCall)`, `TOOL_EVENTS = (onAgentToolCall, onToolResult)`, action models `CommandAction` and `WebhookAction` discriminated by `type`.
- Validators: `server.internal_port` != `server.port`; paths absolute; mounted workdir needs an absolute `path`; clone needs `repo_url`; `claude.extra_args` must not contain `--dangerously-skip-permissions`; webhook url http or https; `matcher` must compile and is only allowed on TOOL_EVENTS; template text placeholders `{{name}}` must be declared in the template's `variables`; plugin ids match `^[^@\s]+@[^@\s]+$` and their marketplace is in `plugins.marketplaces` or a well-known name (`claude-plugins-official`); `sessions.ask_user_question` in forward|deny|allow; `max_sessions >= 1`; config file targets relative without `..`.
- `load_config(path) -> Config`: reads YAML (empty file = all defaults), interpolates with `os.environ`, validates; any failure raises `ConfigError` whose message lists every problem as `<dotted.path>: <message>` (pydantic errors converted).
- `Config.redacted() -> dict`: JSON-able dump where `server.auth_token`, `server.callback_secret`, webhook `secret`s, `claude.env` secrets (via `ClaudeEnv.redacted`), MCP `headers` values and MCP `env` values whose key looks secret are `"***"`.
- `config.example.yaml` at the repo root: every section present with comments explaining each key, auth taken from `${CLAUDE_CODE_OAUTH_TOKEN:-}` / `${ANTHROPIC_API_KEY:-}`, `server.auth_token: ${CCW_AUTH_TOKEN:-}`, at least one example action per hook event (commented where it would need external services), and it loads cleanly with no env vars set.

## Acceptance conditions

- AC1: An empty config loads with the documented defaults [evidence: test:tests/test_config.py::test_minimal_config_defaults]
- AC2: config.example.yaml loads with no env vars set and contains every top-level section [evidence: test:tests/test_config.py::test_full_example_loads]
- AC3: An unknown key fails with its dotted path in the error [evidence: test:tests/test_config.py::test_unknown_key_rejected_with_path]
- AC4: ${VAR} and ${VAR:-default} interpolate string scalars only [evidence: test:tests/test_interpolation.py::test_interpolation_env_and_default]
- AC5: An unset var without default fails naming the YAML path [evidence: test:tests/test_interpolation.py::test_unset_var_error_names_path]
- AC6: $$ escapes to a literal dollar [evidence: test:tests/test_interpolation.py::test_dollar_escape]
- AC7: Hook keys are limited to the 12 event names [evidence: test:tests/test_config.py::test_hook_event_names_validated]
- AC8: Command and webhook actions validate their fields and timeouts [evidence: test:tests/test_config.py::test_webhook_and_command_action_validation]
- AC9: matcher is accepted only on onAgentToolCall and onToolResult and must compile [evidence: test:tests/test_config.py::test_matcher_only_on_tool_events]
- AC10: Workdir mode rules (mounted needs absolute path, clone needs repo_url) hold [evidence: test:tests/test_config.py::test_workdir_mode_rules]
- AC11: --dangerously-skip-permissions in extra_args is rejected [evidence: test:tests/test_config.py::test_dangerous_flag_rejected]
- AC12: Template placeholders must be declared variables [evidence: test:tests/test_config.py::test_template_placeholders_declared]
- AC13: Plugin ids need name@marketplace with a configured marketplace [evidence: test:tests/test_config.py::test_plugin_ids_validated]
- AC14: claude.env is the typed ClaudeEnv model [evidence: test:tests/test_config.py::test_claude_env_section_typed]
- AC15: redacted() masks every secret in the config [evidence: test:tests/test_config.py::test_redacted_masks_secrets]
- AC16: HOOK_EVENTS, VETO_EVENTS and TOOL_EVENTS are exported in design order [evidence: test:tests/test_config.py::test_event_constants]

## Files

- `src/claude_code_wrapper/config.py`
- `src/claude_code_wrapper/interpolation.py`
- `tests/test_config.py`
- `tests/test_interpolation.py`
- `config.example.yaml`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
