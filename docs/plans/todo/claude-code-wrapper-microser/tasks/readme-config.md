---
title: README config
status: todo
repo: claude-code-wrapper
value: "Every config key and Claude env var is documented with type, default and mapping"
files: [README.md, tests/test_readme_config.py]
verify: "uv run pytest tests/test_readme_config.py -q && mise run lint"
depends_on: [readme-api, workbench-example, container]
owns: []
uses: [config-schema, claude-env, container-image]
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

# README config

## Requirement

Write the configuration, deployment and workbench half of `README.md` (D2, D16) plus `tests/test_readme_config.py`, which makes any undocumented config key or Claude env var fail the suite.

README sections (exact H2 headings, added after the readme-api sections): `## Quick start`, `## Configuration reference`, `## Claude Code environment`, `## Docker and compose`, `## Workbench usage`, `## Development`.
- Quick start: compose up with the example config, create a session and a turn with curl, follow SSE, destroy.
- Configuration reference: interpolation rules (`${VAR}`, `${VAR:-default}`, `$$`), validation and exit 78, `ccw check-config`; one table per section with the exact header `| Key | Type | Default | Env var | Description |` and one row per dotted key of the `Config` model (nested models expanded; dict and list valued keys documented at their own path with their item shape described); `Env var` is `-` except for `claude.env.*` rows; a YAML example per section.
- Claude Code environment: how `claude.env` works (typed fields, booleans, secrets, OAuth vs API key precedence, provider exclusivity, managed CLAUDE_CONFIG_DIR, `extra` rules, per-session override at create and resume, redaction) and a table `| Field | Env var | Type | Secret | Group | Description |` with one row per `ClaudeEnv.catalogue()` entry, secrets marked `yes`.
- Docker and compose: build args and pinned versions, the non-root user, volumes `workdir`, `sessions`, `mise` and what lives in each (session dir layout), config mount, env passthrough, ports, healthcheck, the Anthropic policy note on subscription OAuth for third-party products.
- Workbench usage: the example config and compose override, why the workdir path is fixed, plugin install from the mounted directory marketplace, rules copy, OAuth auth, AskUserQuestion forwarding, long-lived sessions and agent-origin turns, git identity and forge tokens.
- Development: `mise run install|test|lint|fmt`, the fake claude, TDD, and a manual real-Claude smoke recipe.
Tests: `test_every_config_key_documented` (walks the pydantic models recursively and asserts a table row for every dotted key), `test_defaults_match_model` (each row's Default cell equals the model default rendered the same way), `test_every_claude_env_var_documented_with_field` (each catalogue entry has a row with its field and env var), `test_secret_env_vars_marked`, `test_sections_present`.

## Acceptance conditions

- AC1: Every config key has a reference row [evidence: test:tests/test_readme_config.py::test_every_config_key_documented]
- AC2: Documented defaults match the model defaults [evidence: test:tests/test_readme_config.py::test_defaults_match_model]
- AC3: Every Claude env var is documented with its field [evidence: test:tests/test_readme_config.py::test_every_claude_env_var_documented_with_field]
- AC4: Secret env vars are marked secret [evidence: test:tests/test_readme_config.py::test_secret_env_vars_marked]
- AC5: Quick start, configuration, environment, docker, workbench and development sections exist [evidence: test:tests/test_readme_config.py::test_sections_present]
- AC6: The configuration reference section exists [evidence: doc-section:README.md#Configuration reference]
- AC7: The workbench usage section exists [evidence: doc-section:README.md#Workbench usage]
- AC8: README.md is plain ASCII [evidence: command:LC_ALL=C grep -q '[^ -~]' README.md && exit 1 || exit 0]
- AC9: The full suite and lint pass [evidence: command:mise run test && mise run lint]

## Files

- `README.md`
- `tests/test_readme_config.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
