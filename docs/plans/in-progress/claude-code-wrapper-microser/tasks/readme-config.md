---
title: README config
status: in-progress
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
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/claude-code-wrapper-microser--readme-config
branch: claude-code-wrapper-microser--readme-config
verified_head: ""
run: claude-code-wrapper-microser
provides: []
acs_for_audit: []
process_gaps: []
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

Settled at the gate (D19-D24, binding):
- Add `## Running in Kubernetes` (no chart): `/healthz` liveness and `/readyz` readiness semantics, SIGTERM grace via `limits.shutdown_timeout_seconds`, JSON logs on stdout, `CCW_CONFIG`, the volumes to back with persistent storage, and the trust model reference.
- Document that secret env overrides are not persisted and must be resent on resume, and that DELETE keeps files unless `purge=true`.

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

- AC10: The README covers running in Kubernetes [evidence: doc-section:README.md#Running in Kubernetes]
- AC11: The README says secret overrides must be resent on resume [evidence: command:grep -qi 'resent' README.md]

## Files

- `README.md`
- `tests/test_readme_config.py`

## Interfaces

- Reads `Config` models, `ClaudeEnv.catalogue()`, `Dockerfile`, compose files and examples; writes only README.md and its test.

## Expected outcome

Every config.yaml key and every Claude Code env var the service models is documented with type, default and env mapping, and the README explains running it with docker and with the workbench.

## Steps

- Write `tests/test_readme_config.py`; red; write the sections (generate the tables from the models once, then edit descriptions by hand); green; `mise run test` and `mise run lint`.

## Notes

- Render defaults the way the test does (for example `""`, `[]`, `{}`, `true`, numbers as written); agree the rendering in one helper inside the test.
- Keep sections written by readme-api untouched except for a table of contents if you add one.

- 2026-10-20 run claude-code-wrapper-microser: dependency-failed on readme-api, workbench-example, container
