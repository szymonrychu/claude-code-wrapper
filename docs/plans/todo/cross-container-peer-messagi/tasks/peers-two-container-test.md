---
title: Peers two container test
status: todo
repo: .
value: CI proves signed peer messaging across a real container boundary on a shared volume
files: [docker-compose.peers-ci.yml, scripts/integration-peers.sh, mise.toml, tests/integration_peers/__init__.py, tests/integration_peers/conftest.py, tests/integration_peers/test_peers_containers.py, tests/integration_peers/config.ci.yaml, tests/integration_peers/scenario.json]
verify: "bash scripts/integration-peers.sh && mise run lint"
depends_on: [peers-image-compose-docs, manager-peers-lifecycle, fake-claude-peer-harness]
owns: []
uses: [peers-image, fake-match-substitution]
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

# Peers two container test

## Requirement

Docker two-container integration test, run in CI by `mise run test:integration` (design D12).

- docker-compose.peers-ci.yml (repo root): a CI override for examples/docker-compose.peers.yml, used as `docker compose -p ccw-it-peers -f examples/docker-compose.peers.yml -f docker-compose.peers-ci.yml`. Compose resolves every file's relative paths against the first file's directory (examples/), so all host paths here are written relative to examples/ and the example keeps `build: ..`: on both services mount `../tests/fake_claude` at `/opt/fake-claude:ro`, `../tests/integration/claude-shim.sh` at `/opt/ci/claude:ro`, `../tests/integration_peers/scenario.json` at `/opt/ci/scenario.json:ro` and `../tests/integration_peers/config.ci.yaml` at `/etc/ccw/config.yaml:ro`; empty real credentials; `CCW_AUTH_TOKEN: ci-token`; `CCW_PEER_TOKEN: ci-peer-token-0123456789`; `restart: "no"`.
- tests/integration_peers/config.ci.yaml: the CI integration config (as tests/integration/config.ci.yaml: fake claude at /opt/ci/claude, `FAKE_CLAUDE_SCENARIO`) plus `peers` enabled with name, url and token from the env vars the compose file sets, `heartbeat_seconds: 1`, `stale_seconds: 5`.
- tests/integration_peers/scenario.json: the same three body-anchored turns as the in-process e2e (`^send (?P<body>ping) to (?P<to>\S+)$`, the `<agent-message ...>\nping\n</agent-message>` reply with `pong`, `^list peers$`) using `{{name}}` substitution, so one scenario serves both containers and the exchange cannot loop.
- tests/integration_peers/__init__.py and conftest.py: every test here is marked `integration` (so `mise run test` deselects it); fixtures for httpx clients on `CCW_PEER_A_URL` and `CCW_PEER_B_URL` (defaults `http://127.0.0.1:8181` / `8182`) with bearer `ci-token`; an unreachable service fails hard when `CCW_INTEGRATION_TASK=1`, else skips.
- tests/integration_peers/test_peers_containers.py: create a session in each container through the API; A's `list peers` tool result, read from A's `tool_result` events through the session events API, lists `b/<B id>`; `send ping to b/<B id>` to A gives B a peer turn framed from `a/<A id>` with `ping`, A a peer turn with B's `pong`, and A an idle-notice turn; each side sent exactly one message; every wait bounded (at most 60 s) and naming what it waited for.
- scripts/integration-peers.sh (executable, `set -euo pipefail`, same shape as scripts/integration.sh): builds the image (`docker compose ... build ccw-a`) unless `CCW_SKIP_BUILD=1`; `up -d` without `--build`; waits for `/healthz` and `/readyz` on both ports (bounded by `CCW_HEALTH_TIMEOUT`, default 120); runs `uv run pytest tests/integration_peers -m integration -p no:cacheprovider` with `CCW_INTEGRATION_TASK=1`; prints compose logs on failure; always `down -v --remove-orphans`.
- mise.toml: new task `test:integration:peers` running `scripts/integration-peers.sh`; `test:integration` runs `scripts/integration.sh` (which builds `claude-code-wrapper:local`) and then `CCW_SKIP_BUILD=1 scripts/integration-peers.sh`, so CI (which runs `mise run test:integration`) builds the image once and runs both. tests/test_ci_workflows.py stays green unchanged.

Interfaces: none new.

Notes: the in-process suite already proves the logic; this task proves the container boundary: the shared named volume, the pre-created `/peers` ownership, service-name advertise urls, signed requests between containers and the image. Real-claude acceptance of the tool schemas is cannotVerify (no credentials), not part of this task.

## Acceptance conditions

- AC1: the two-container run lists, sends, replies once and delivers the idle notice across containers [evidence: command:bash scripts/integration-peers.sh]
- AC2: mise test:integration runs both integration scripts with the peers build skipped, and test:integration:peers exists [evidence: command:mise tasks info test:integration | grep -q 'CCW_SKIP_BUILD=1 scripts/integration-peers.sh' && mise tasks info test:integration:peers]
- AC3: the integration_peers tests are deselected by the default test run [evidence: command:uv run pytest tests/integration_peers -q -m 'not integration' --co 2>&1 | grep -q 'deselected']
- AC4: the CI workflow and precommit suites stay green [evidence: command:uv run pytest tests/test_ci_workflows.py tests/test_precommit_config.py -q]

## Files

- `docker-compose.peers-ci.yml`
- `scripts/integration-peers.sh`
- `mise.toml`
- `tests/integration_peers/__init__.py`
- `tests/integration_peers/conftest.py`
- `tests/integration_peers/test_peers_containers.py`
- `tests/integration_peers/config.ci.yaml`
- `tests/integration_peers/scenario.json`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
