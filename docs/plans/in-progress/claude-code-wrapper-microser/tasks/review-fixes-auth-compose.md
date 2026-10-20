---
title: Review fixes auth compose
status: in-progress
repo: claude-code-wrapper
value: "The API never runs unauthenticated by accident; compose is safe by default"
files: [src/claude_code_wrapper/config.py, src/claude_code_wrapper/api/app.py, src/claude_code_wrapper/service.py, docker-compose.yml, .env.example, src/claude_code_wrapper/data/config.example.yaml, README.md, tests/test_api_auth.py]
verify: "uv run pytest tests/test_api_auth.py tests/test_config.py tests/test_container.py tests/test_readme_config.py -q && mise run lint"
depends_on: [audit-fixes-container-home-s]
owns: []
uses: []
model: ""
estimate: 60
actual: ""
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/claude-code-wrapper-microser--review-fixes-auth-compose
branch: claude-code-wrapper-microser--review-fixes-auth-compose
verified_head: ""
run: ""
provides: []
---

# Review fixes auth compose

## Requirement

Epic-review remediation (security review). The service must not silently run without API auth.

- Config: add `server.allow_anonymous: bool = false`. `ccw serve` (and create_app) refuse to start when `server.auth_token` is empty unless `allow_anonymous` is true; the error names both keys. With allow_anonymous true and no token, keep today's behaviour (warning logged, /v1 open). /healthz, /readyz and /metrics stay unauthenticated by design (cluster-internal Prometheus scrape; record this in the README trust model).
- docker-compose.yml: require the token (`CCW_AUTH_TOKEN: ${CCW_AUTH_TOKEN:?set CCW_AUTH_TOKEN}` or equivalent) and publish the port on loopback by default (`"127.0.0.1:${CCW_PORT:-8080}:8080"`). Keep .env.example in sync.
- Update the bundled example config (src/claude_code_wrapper/data/config.example.yaml), the workbench example if it sets server keys, and the README configuration reference row for the new key (the README coverage tests enforce this) and the Quick start so a first run sets a token.

## Acceptance conditions

- AC1: Empty auth_token without allow_anonymous makes startup fail with an error naming server.auth_token and server.allow_anonymous [evidence: test:tests/test_api_auth.py::test_empty_token_refused_without_allow_anonymous]
- AC2: allow_anonymous true with empty token starts with /v1 open and a warning [evidence: test:tests/test_api_auth.py::test_allow_anonymous_opens_v1]
- AC3: allow_anonymous is a validated config key with default false [evidence: test:tests/test_config.py::test_allow_anonymous_default_false]
- AC4: Compose requires CCW_AUTH_TOKEN and binds the published port to 127.0.0.1 by default [evidence: test:tests/test_container.py::test_compose_requires_token_and_binds_loopback]
- AC5: README documents server.allow_anonymous and that health/ready/metrics are unauthenticated by design [evidence: test:tests/test_readme_config.py::test_every_config_key_documented]

## Files

- `src/claude_code_wrapper/config.py`
- `src/claude_code_wrapper/api/app.py`
- `src/claude_code_wrapper/service.py`
- `docker-compose.yml`
- `.env.example`
- `src/claude_code_wrapper/data/config.example.yaml`
- `README.md`
- `tests/test_api_auth.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

Every acceptance condition above passes on main after merge; full `mise run test` and `mise run lint` stay green.

What is true and observable when the task is done.

## Steps

1. Red: add/rename the evidence tests and see the new ones fail for the stated reason.
2. Green: smallest change that passes them.
3. Refactor with the suite green; run `mise run test` and `mise run lint`.

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
