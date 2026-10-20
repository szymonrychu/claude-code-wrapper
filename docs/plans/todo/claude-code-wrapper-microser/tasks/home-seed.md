---
title: Home seed
status: todo
repo: claude-code-wrapper
value: "Sessions boot without onboarding or trust prompts, with MCP and instructions"
files: [src/claude_code_wrapper/bootstrap/__init__.py, src/claude_code_wrapper/bootstrap/claude_json.py, src/claude_code_wrapper/bootstrap/instructions.py, tests/test_bootstrap_home.py]
verify: "uv run pytest tests/test_bootstrap_home.py -q && mise run lint"
depends_on: [config-models]
owns: [home-seed]
uses: [config-schema]
model: ""
estimate: 60
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Home seed

## Requirement

Render the session `CLAUDE_CONFIG_DIR` files that are not settings.json (D13, D14, D11): `src/claude_code_wrapper/bootstrap/__init__.py` (package, re-exports), `bootstrap/claude_json.py`, `bootstrap/instructions.py`.

`ensure_config_dir(config_dir)`: creates it with mode 0700.

`seed_claude_json(config_dir, workdir, claude_cfg, env: ClaudeEnv)` writes `<config_dir>/.claude.json`:
- Loads an existing file and merges (never drops unrelated keys claude itself wrote, so resume keeps its state).
- Sets `hasCompletedOnboarding: true`, `autoUpdates: false`, and `bypassPermissionsModeAccepted: true` when `claude_cfg.permission_mode == "bypassPermissions"`.
- `projects[<os.path.realpath(workdir)>].hasTrustDialogAccepted = true`, and the same for the unresolved absolute path when it differs (macOS `/tmp` vs `/private/tmp`).
- When `env.anthropic_api_key` is set: `customApiKeyResponses.approved` contains its last 20 characters once (no duplicates); `rejected` untouched.
- `mcpServers`: one entry per `claude.mcp_servers` item: stdio `{type: "stdio", command, args, env}`, http/sse `{type, url, headers}`; entries not configured are kept.
- Deep-merges `claude.claude_json` last (dicts merged recursively, other values replaced).
- Writes atomically (temp file in the same dir, fsync, rename) with mode 0600.

`write_instructions(config_dir, prompts_cfg)` writes `<config_dir>/CLAUDE.md` = the content of `prompts.global_claude_md` (when set; missing file raises `BootstrapError`) followed by a blank line and `prompts.system_append`, prefixed with the marker line `<!-- written by claude-code-wrapper -->`. When both are empty and an existing CLAUDE.md starts with the marker, it is removed; a user-provided CLAUDE.md without the marker is never removed.

`copy_config_files(config_dir, claude_cfg)`: each `{source, target}` copies a file or a directory tree (recursively, overwriting) to `<config_dir>/<target>`; a target that is absolute or resolves outside config_dir raises `BootstrapError`; a missing source raises `BootstrapError` naming it.

Nothing is ever written into the workdir.

## Acceptance conditions

- AC1: The seed sets onboarding, autoUpdates false and trust for the workdir [evidence: test:tests/test_bootstrap_home.py::test_seed_creates_onboarding_and_trust]
- AC2: Trust is recorded for the resolved and the given path [evidence: test:tests/test_bootstrap_home.py::test_trust_uses_resolved_path]
- AC3: Existing keys in .claude.json survive a re-seed [evidence: test:tests/test_bootstrap_home.py::test_seed_merges_existing_keys]
- AC4: The API key suffix is approved exactly once [evidence: test:tests/test_bootstrap_home.py::test_api_key_suffix_approved_once]
- AC5: bypassPermissionsModeAccepted is set only for bypassPermissions mode [evidence: test:tests/test_bootstrap_home.py::test_bypass_accepted_only_in_bypass_mode]
- AC6: MCP servers render into mcpServers by type [evidence: test:tests/test_bootstrap_home.py::test_mcp_servers_rendered]
- AC7: The claude_json overlay deep-merges last [evidence: test:tests/test_bootstrap_home.py::test_overlay_deep_merge]
- AC8: .claude.json is written with mode 0600 [evidence: test:tests/test_bootstrap_home.py::test_file_mode_0600]
- AC9: CLAUDE.md concatenates the global file and system_append with the marker [evidence: test:tests/test_bootstrap_home.py::test_instructions_concat_global_and_append]
- AC10: An empty prompts config removes only a marker CLAUDE.md [evidence: test:tests/test_bootstrap_home.py::test_instructions_absent_when_empty]
- AC11: Config files copy into the config dir and escaping targets are rejected [evidence: test:tests/test_bootstrap_home.py::test_config_files_copied_and_escape_rejected]
- AC12: No file is created in the workdir [evidence: test:tests/test_bootstrap_home.py::test_workdir_untouched]

## Files

- `src/claude_code_wrapper/bootstrap/__init__.py`
- `src/claude_code_wrapper/bootstrap/claude_json.py`
- `src/claude_code_wrapper/bootstrap/instructions.py`
- `tests/test_bootstrap_home.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
