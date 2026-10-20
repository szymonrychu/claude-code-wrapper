---
title: Claude env model
status: in-progress
repo: claude-code-wrapper
value: "Every Claude Code env var configurable as a validated, redacted field (D2)"
files: [src/claude_code_wrapper/claude_env.py, src/claude_code_wrapper/redact.py, tests/test_claude_env.py, tests/test_redact.py]
verify: "uv run pytest tests/test_claude_env.py tests/test_redact.py -q && mise run lint"
depends_on: [repo-scaffold]
owns: [claude-env, redactor]
uses: []
model: ""
estimate: 75
actual: ""
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/claude-code-wrapper-microser--claude-env-model
branch: claude-code-wrapper-microser--claude-env-model
verified_head: ""
run: ""
provides: []
---

# Claude env model

## Requirement

Build the typed Claude Code environment model (user decision D2) and the shared redactor.

`src/claude_code_wrapper/claude_env.py`:
- `ClaudeEnv` pydantic v2 model, `extra="forbid"`. One optional field per known Claude Code env var; the field name is the env var name lowercased (for example `claude_code_oauth_token`, `anthropic_api_key`, `disable_telemetry`). Each field carries metadata exposed through `ClaudeEnv.catalogue() -> list[EnvVarSpec]` where `EnvVarSpec` has `field`, `env`, `group` (one of auth, provider, model, behaviour, mcp, network, telemetry, plugins, privacy), `secret: bool`, `managed: bool`, `type` (str, bool, int, secret) and a one-line `description`.
- The catalogue includes at least: CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_BASE_URL, ANTHROPIC_CUSTOM_HEADERS, ANTHROPIC_MODEL, ANTHROPIC_SMALL_FAST_MODEL, ANTHROPIC_DEFAULT_OPUS_MODEL, ANTHROPIC_DEFAULT_SONNET_MODEL, ANTHROPIC_DEFAULT_HAIKU_MODEL, CLAUDE_CODE_SUBAGENT_MODEL, CLAUDE_CODE_USE_BEDROCK, CLAUDE_CODE_USE_VERTEX, CLAUDE_CODE_USE_FOUNDRY, CLAUDE_CODE_USE_ANTHROPIC_AWS, AWS_REGION, AWS_PROFILE, AWS_BEARER_TOKEN_BEDROCK, ANTHROPIC_BEDROCK_BASE_URL, CLOUD_ML_REGION, ANTHROPIC_VERTEX_PROJECT_ID, ANTHROPIC_VERTEX_BASE_URL, ANTHROPIC_FOUNDRY_RESOURCE, ANTHROPIC_FOUNDRY_API_KEY, ANTHROPIC_FOUNDRY_BASE_URL, CLAUDE_CODE_SKIP_BEDROCK_AUTH, CLAUDE_CODE_SKIP_VERTEX_AUTH, CLAUDE_CODE_SKIP_FOUNDRY_AUTH, CLAUDE_CONFIG_DIR, CLAUDE_CODE_MAX_OUTPUT_TOKENS, MAX_THINKING_TOKENS, BASH_DEFAULT_TIMEOUT_MS, BASH_MAX_TIMEOUT_MS, BASH_MAX_OUTPUT_LENGTH, MCP_TIMEOUT, MCP_TOOL_TIMEOUT, MAX_MCP_OUTPUT_TOKENS, CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH, DISABLE_TELEMETRY, DISABLE_ERROR_REPORTING, DISABLE_AUTOUPDATER, DISABLE_COST_WARNINGS, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC, CLAUDE_CODE_DISABLE_AUTO_MEMORY, CLAUDE_CODE_ENABLE_TELEMETRY, CLAUDE_AUTOCOMPACT_PCT_OVERRIDE, CLAUDE_CODE_PROMPT_CACHE_TTL, ENABLE_CLAUDEAI_MCP_SERVERS, CLAUDE_CODE_SYNC_PLUGIN_INSTALL, CLAUDE_CODE_PLUGIN_SEED_DIR, CLAUDE_CODE_SKIP_PROMPT_HISTORY, CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS, HTTP_PROXY, HTTPS_PROXY, NO_PROXY. Verify the list against https://code.claude.com/docs/en/env-vars and the env section of https://code.claude.com/docs/en/settings; add every other documented variable that the service does not manage, and record the date the doc was checked in the module docstring.
- Types: secrets (OAuth token, API key, auth token, custom headers, Bedrock bearer token, Foundry API key) are `SecretStr`; flags are `bool` (True renders `"1"`, False and None are omitted); counts, token limits and millisecond timeouts are positive ints; `claude_autocompact_pct_override` is 1..100; vars whose documented values are words (for example ENABLE_CLAUDEAI_MCP_SERVERS `true|false`, CLAUDE_CODE_PROMPT_CACHE_TTL like `1h`) are validated strings.
- `claude_config_dir` is `managed=True`: setting it raises a validation error whose message says CLAUDE_CONFIG_DIR is set per session by the service from `paths.sessions_root`.
- `extra: dict[str, str]` passthrough: keys must match `^[A-Z_][A-Z0-9_]*$`; a key equal to a modelled var is rejected with a message naming the field to use; managed vars, `CCW_*`, `TERM`, `HOME` are rejected.
- `to_env() -> dict[str, str]`: modelled fields that are set plus `extra`. When both `claude_code_oauth_token` and `anthropic_api_key` are set, `ANTHROPIC_API_KEY` is dropped and `warnings()` returns a message saying the OAuth token wins.
- Provider exclusivity: at most one of use_bedrock, use_vertex, use_foundry, use_anthropic_aws may be true (validation error).
- `merged(override: ClaudeEnv) -> ClaudeEnv`: fields the override explicitly set win (including False, which unsets a flag); `extra` merges key-wise with the override winning.
- `redacted() -> dict`: field values with secrets as `"***"`; `extra` values whose key matches `TOKEN|KEY|SECRET|PASSWORD|AUTH|CREDENTIAL` are also `"***"`.
- `secret_values() -> list[str]`: all secret values (fields and secret-looking extra), for the redactor.

`src/claude_code_wrapper/redact.py`:
- `Redactor(values: Iterable[str] = (), patterns: Iterable[str] = DEFAULT_PATTERNS)`: `redact(text)` replaces every value of 8 or more characters longest-first with `***`, then applies regex patterns for `sk-ant-[A-Za-z0-9_-]+`, `Bearer\s+[A-Za-z0-9._~+/=-]+`, GitHub tokens `gh[pousr]_[A-Za-z0-9]{20,}`, GitLab `glpat-[A-Za-z0-9_-]{20,}`, and URL basic auth `://user:pass@` (password part masked). `redact_obj(obj)` returns a copy with every string inside dicts and lists redacted. `add(values)` extends the value set. Never raises on non-str input (returns it unchanged).

## Acceptance conditions

- AC1: Every env var from the D2 minimum list is in the catalogue with its env name [evidence: test:tests/test_claude_env.py::test_every_required_var_is_modelled]
- AC2: to_env maps set fields to their env var names [evidence: test:tests/test_claude_env.py::test_to_env_maps_fields_to_env_names]
- AC3: True renders 1 and False or unset is omitted [evidence: test:tests/test_claude_env.py::test_bool_true_is_1_false_omitted]
- AC4: Numeric fields reject zero and negative values [evidence: test:tests/test_claude_env.py::test_numeric_fields_reject_non_positive]
- AC5: extra rejects a key that shadows a modelled var and names the field [evidence: test:tests/test_claude_env.py::test_extra_rejects_shadowing_modelled_var]
- AC6: extra rejects managed, CCW_ and malformed keys [evidence: test:tests/test_claude_env.py::test_extra_rejects_managed_and_ccw_vars]
- AC7: CLAUDE_CONFIG_DIR is catalogued as managed and cannot be configured [evidence: test:tests/test_claude_env.py::test_claude_config_dir_is_service_managed]
- AC8: merged applies override fields and extra key-wise [evidence: test:tests/test_claude_env.py::test_override_merge_fieldwise_and_extra]
- AC9: OAuth token wins over API key with a warning [evidence: test:tests/test_claude_env.py::test_oauth_token_wins_over_api_key]
- AC10: Provider flags are mutually exclusive [evidence: test:tests/test_claude_env.py::test_provider_flags_are_exclusive]
- AC11: redacted masks secret fields and secret-looking extra keys [evidence: test:tests/test_claude_env.py::test_redacted_masks_secrets]
- AC12: Redactor replaces longest values first [evidence: test:tests/test_redact.py::test_redactor_longest_first]
- AC13: Redactor masks token patterns and URL basic auth [evidence: test:tests/test_redact.py::test_redactor_patterns]
- AC14: redact_obj walks nested dicts and lists [evidence: test:tests/test_redact.py::test_redact_obj_recursive]
- AC15: The catalogue was checked against the official env-vars doc and every documented non-managed variable is modelled, with the check date in the docstring [evidence: judgement:]

## Files

- `src/claude_code_wrapper/claude_env.py`
- `src/claude_code_wrapper/redact.py`
- `tests/test_claude_env.py`
- `tests/test_redact.py`

## Interfaces

- `claude_code_wrapper.claude_env.ClaudeEnv` with `catalogue()`, `to_env()`, `merged()`, `redacted()`, `secret_values()`, `warnings()`; `EnvVarSpec` dataclass. Part `claude-env`, consumed by config-models (`claude.env`), session-boot (child env), session-manager (override merge), api-sessions (create body), readme-config (reference table).
- `claude_code_wrapper.redact.Redactor` with `redact()`, `redact_obj()`, `add()`; `DEFAULT_PATTERNS`. Part `redactor`.

## Expected outcome

A config author writes `claude: {env: {claude_code_oauth_token: ${CLAUDE_CODE_OAUTH_TOKEN}, disable_autoupdater: true, extra: {WORKBENCH_ROOT: /workdir/workbench}}}` and gets a validated model that renders the right env and never leaks the token when dumped.

## Steps

- Write `tests/test_claude_env.py` and `tests/test_redact.py` first; run `uv run pytest tests/test_claude_env.py tests/test_redact.py -q` and see import failures.
- Implement redact.py; its tests pass.
- Implement claude_env.py field by field; run the tests to green.
- Check the official env-vars doc, add missing documented vars with tests still green; `mise run lint`.

## Notes

- Sources: tatara `W/internal/transcript/redact.go` (longest-first, env-derived secrets), magic `M/pty_proc.py:133-152` (OAuth wins, MAX_THINKING_TOKENS set or popped), ros2 `R/runner.py:234-255` (explicit child env).
- `DISABLE_NON_ESSENTIAL_MODEL_CALLS` is not in the 2.1.304 binary; do not add it unless the doc lists it.
- Field metadata (`json_schema_extra` or `Field(..., description=...)`) is what readme-config's coverage test reads; keep descriptions short and ASCII.
