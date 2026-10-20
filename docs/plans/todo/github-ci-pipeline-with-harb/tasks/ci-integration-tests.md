---
title: CI integration tests
status: todo
repo: .
value: The built container is proven to serve and run a full session before any image is pushed
files: [mise.toml, pyproject.toml, scripts/integration.sh, docker-compose.ci.yml, tests/integration/config.ci.yaml, tests/integration/conftest.py, tests/integration/test_container_http.py]
verify: "mise run test:integration && mise run lint && mise run test"
depends_on: [ci-precommit]
owns: []
uses: []
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

# CI integration tests

## Requirement

Add integration tests that exercise the real container over HTTP, run by a single mise task.

- `mise run test:integration` (script `scripts/integration.sh`, shellcheck-clean, `set -euo pipefail`): `docker compose -f docker-compose.yml -f docker-compose.ci.yml up -d --build ccw`, bounded health wait (poll `GET /healthz` then `/readyz` on the published port, up to 120 s, hard fail with `docker compose logs` on timeout), then `uv run pytest tests/integration -m integration -p no:cacheprovider`, `trap` doing `docker compose logs` on failure and `docker compose down -v` on every exit. Port from `CCW_PORT` (default 8080); compose project name fixed via `-p ccw-it` so it never collides with a dev stack.
- `docker-compose.ci.yml` override: mounts `tests/fake_claude` read-only into the container (at `/opt/fake-claude`), a CI config file `tests/integration/config.ci.yaml` (copied from config.example.yaml semantics) setting `claude.binary` to a `claude` shim script that execs the fake (`/app/.venv/bin/python -I /opt/fake-claude/fake_claude.py`), sets `server.auth_token: ci-token`, and a scenario file for the fake; no real Claude credentials needed. Reuse the shim and scenario conventions of tests/conftest.py and tests/test_e2e_lifecycle.py.
- `tests/integration/` tests (marker `integration`, registered in pyproject `[tool.pytest.ini_options] markers`; default `mise run test` and `uv run pytest` exclude them via `addopts = "-q -m 'not integration'"`), using httpx against `CCW_BASE_URL` (default http://127.0.0.1:8080): healthz and readyz 200; public routes return 401 without the bearer token and 200 with it; GET config returns the effective config with secrets redacted (the auth token and callback secret never appear in the body); GET sessions returns an empty list; and a full lifecycle: create a session, wait until ready, send a turn, read its result/events through the fake claude, destroy the session, confirm it is gone. Read src/claude_code_wrapper/api for the exact routes and payloads before writing tests.
- The tests skip with a clear message (not fail) when `CCW_BASE_URL` is unreachable only if run outside the task; inside `mise run test:integration` an unreachable service is a hard failure.
- Document the task in mise.toml description and in the README of the repo CI section is owned by ci-workflows.

## Acceptance conditions

- AC1: mise.toml defines test:integration running scripts/integration.sh, which brings the stack up with the CI override, waits boundedly for health, runs the integration suite and always runs compose down -v [evidence: command:bash -c 'grep -q "tests/integration" scripts/integration.sh && grep -q "down -v" scripts/integration.sh && grep -q "test:integration" mise.toml']
- AC2: integration tests are marked integration and excluded from the default suite [evidence: command:test "$(uv run pytest --collect-only -q 2>&1 | grep -c tests/integration)" = 0]
- AC3: the integration suite passes against the real built container with the fake claude [evidence: command:mise run test:integration]
- AC4: the integration tests cover health, auth 401, config redaction, sessions list and a full create-turn-destroy lifecycle [evidence: command:uv run pytest tests/integration --collect-only -q | grep -c "::" | grep -qE "^([5-9]|[1-9][0-9])$"]
- AC5: scripts/integration.sh is shellcheck clean and lint and the default suite stay green [evidence: command:shellcheck scripts/integration.sh && mise run lint && mise run test]

## Files

- `mise.toml`
- `pyproject.toml`
- `scripts/integration.sh`
- `docker-compose.ci.yml`
- `tests/integration/config.ci.yaml`
- `tests/integration/conftest.py`
- `tests/integration/test_container_http.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
