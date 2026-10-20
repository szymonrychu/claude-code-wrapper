---
title: Plugins and mise
status: todo
repo: claude-code-wrapper
value: "Plugins and tools pre-installed per config, the workbench plugin included"
files: [src/claude_code_wrapper/plugins.py, src/claude_code_wrapper/tooling.py, tests/test_plugins.py, tests/test_tooling.py]
verify: "uv run pytest tests/test_plugins.py tests/test_tooling.py -q && mise run lint"
depends_on: [config-models, fake-claude]
owns: [plugin-installer, mise-tooling]
uses: [config-schema, fake-claude]
model: ""
estimate: 75
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Plugins and mise

## Requirement

Build plugin pre-install and mise tooling (D12, D11) in `src/claude_code_wrapper/plugins.py` and `src/claude_code_wrapper/tooling.py`.

plugins.py:
- `async install_plugins(plugins_cfg, *, claude_binary, config_dir, workdir, env) -> PluginReport` (`installed`, `already_present`, `warnings`).
- Every command runs with `env` + `CLAUDE_CONFIG_DIR=<config_dir>` + `DISABLE_AUTOUPDATER=1`, cwd = workdir, timeout `plugins.timeout_seconds`, stdin closed.
- Order: `claude plugin marketplace list --json`; for each configured marketplace whose name is absent: `claude plugin marketplace add <source> --scope user --json` (a source that looks like a local path must be an existing directory, else `PluginInstallError` before running anything); `claude plugin list --json`; for each `install` item not installed and enabled: `claude plugin install <id> -s <scope> -y --json`; finally `claude plugin list --json` and check every required id is enabled.
- A required failure raises `PluginInstallError(plugin_id, exit_code, output_tail)` with redacted output; an optional failure adds a warning.
- Idempotent: a second run on the same config dir runs only the list commands.

tooling.py:
- `render_global_mise(mise_cfg) -> Path | None`: with `config_file` set copies it to `global_config_path`; else with `tools` writes a TOML `[tools]` table (`name = "version"`) there; returns None when neither is set.
- `async install_global(mise_cfg, env) -> None`: when `install_on_start` and a global config was rendered, runs `mise install` with `MISE_GLOBAL_CONFIG_FILE=<path>` and `MISE_YES=1`, timeout `install_timeout_seconds`; failure raises `ToolingError` when `required`, else logs a warning.
- `async install_workdir(mise_cfg, workdir, env) -> bool`: when `install_workdir` and the workdir has `mise.toml` or `.mise.toml`: `mise trust <file>` then `mise install` in the workdir; returns whether it ran; failure as above.
- `mise_env(mise_cfg) -> dict[str, str]`: `MISE_GLOBAL_CONFIG_FILE` (when rendered), `MISE_TRUSTED_CONFIG_PATHS` (trusted_paths joined with `:`), `MISE_YES=1`, and `MISE_DATA_DIR` copied from the service env when present, so the claude child's Bash tool sees the same tools.
- The mise binary is resolved from PATH (`mise`); a missing binary when mise work is configured raises `ToolingError`.

## Acceptance conditions

- AC1: Missing marketplaces are added and plugins installed with -y --json [evidence: test:tests/test_plugins.py::test_adds_missing_marketplace_and_installs]
- AC2: A second run executes no add or install commands [evidence: test:tests/test_plugins.py::test_idempotent_second_run_no_commands]
- AC3: A required plugin failure raises with a redacted output tail [evidence: test:tests/test_plugins.py::test_required_failure_raises_with_tail]
- AC4: An optional plugin failure becomes a warning [evidence: test:tests/test_plugins.py::test_optional_failure_warns]
- AC5: A local marketplace dir must exist before any command runs [evidence: test:tests/test_plugins.py::test_local_marketplace_dir_must_exist]
- AC6: Commands run with CLAUDE_CONFIG_DIR of the session [evidence: test:tests/test_plugins.py::test_config_dir_env_passed]
- AC7: The global mise TOML renders from tools or config_file [evidence: test:tests/test_tooling.py::test_render_global_toml]
- AC8: install_global runs mise install with MISE_GLOBAL_CONFIG_FILE [evidence: test:tests/test_tooling.py::test_install_global_invokes_mise_install]
- AC9: install_workdir trusts then installs when a mise config exists [evidence: test:tests/test_tooling.py::test_install_workdir_trusts_then_installs]
- AC10: install_workdir does nothing without a mise config [evidence: test:tests/test_tooling.py::test_install_workdir_noop_without_config]
- AC11: A required mise failure raises ToolingError [evidence: test:tests/test_tooling.py::test_failure_required_raises]
- AC12: mise_env exposes the vars the claude child needs [evidence: test:tests/test_tooling.py::test_mise_env_vars]

## Files

- `src/claude_code_wrapper/plugins.py`
- `src/claude_code_wrapper/tooling.py`
- `tests/test_plugins.py`
- `tests/test_tooling.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
