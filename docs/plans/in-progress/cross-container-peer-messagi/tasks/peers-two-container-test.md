---
title: Peers two container test
status: in-progress
repo: .
value: "CI proves join, messaging and leave across a real container boundary"
files: [docker-compose.peers-ci.yml, scripts/integration-peers.sh, mise.toml, tests/integration_peers/__init__.py, tests/integration_peers/conftest.py, tests/integration_peers/test_peers_containers.py, tests/integration_peers/config.ci.yaml, tests/integration_peers/scenario.json]
verify: "bash scripts/integration-peers.sh && mise run lint"
depends_on: [peers-compose-example, peers-join-leave-order, fake-claude-peer-harness]
owns: []
uses: [peers-compose, fake-match-substitution]
model: sonnet
estimate: 75
actual: ""
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/cross-container-peer-messagi--peers-two-container-test
branch: cross-container-peer-messagi--peers-two-container-test
verified_head: ""
run: ""
provides: []
---

# Peers two container test

## Requirement

Docker two-container integration test, run in CI by `mise run test:integration` (design D12).

- docker-compose.peers-ci.yml (repo root): a CI override for examples/docker-compose.peers.yml, used as `docker compose -p ccw-it-peers -f examples/docker-compose.peers.yml -f docker-compose.peers-ci.yml`. Compose resolves every file's relative paths against the first file's directory (examples/), so all host paths here are relative to examples/ and the example keeps `build: ..`: on both services mount `../tests/fake_claude` at `/opt/fake-claude:ro`, `../tests/integration/claude-shim.sh` at `/opt/ci/claude:ro`, `../tests/integration_peers/scenario.json` at `/opt/ci/scenario.json:ro` and `../tests/integration_peers/config.ci.yaml` at `/etc/ccw/config.yaml:ro`; empty real credentials; `CCW_AUTH_TOKEN: ci-token`; `CCW_PEER_TOKEN: ci-peer-token-0123456789`; `restart: "no"`.
- tests/integration_peers/config.ci.yaml: the CI integration config (as tests/integration/config.ci.yaml: fake claude at /opt/ci/claude, `FAKE_CLAUDE_SCENARIO`) plus `peers` enabled with name, url, seed and token from the env vars the compose file sets, `heartbeat_seconds: 1`, `max_missed_heartbeats: 3`, `join_timeout_seconds: 5`, and a postJoin and preLeave command hook appending to `/sessions/peer-hooks.log` so the test can read the order through `docker compose exec`.
- tests/integration_peers/scenario.json: the same three body-anchored turns as the in-process e2e, so one scenario serves both containers and the exchange cannot loop.
- tests/integration_peers/__init__.py and conftest.py: every test here is marked `integration` (so `mise run test` deselects it); fixtures for httpx clients on `CCW_PEER_A_URL` and `CCW_PEER_B_URL` (defaults `http://127.0.0.1:8181` / `8182`) with bearer `ci-token`, and a `compose(*args)` helper running `docker compose` with the project and files from `CCW_PEERS_COMPOSE_ARGS` (set by the script); an unreachable service fails hard when `CCW_INTEGRATION_TASK=1`, else skips.
- tests/integration_peers/test_peers_containers.py: both containers joined (postJoin in each hook log); create a session in each; A's `list peers` tool result (from A's `tool_result` events through the session events API) lists `b/<B id>` and B's lists `a/<A id>`; `send ping to b/<B id>` on A gives B a peer turn framed from `a/<A id>`, A a `pong` turn and an idle-notice turn; the same from B to A; each side sent exactly one message per direction; then `compose("stop", "ccw-b")` and A's listing drops B within 2 heartbeat intervals (through its `leaving: true` heartbeat), with B's hook log showing preLeave. Every wait is bounded (at most 60 s) and names what it waited for.
- scripts/integration-peers.sh (executable, `set -euo pipefail`, same shape as scripts/integration.sh): builds the image (`docker compose ... build ccw-a`) unless `CCW_SKIP_BUILD=1`; `up -d` without `--build`; waits for `/healthz` and `/readyz` on both ports (bounded by `CCW_HEALTH_TIMEOUT`, default 120); exports `CCW_PEERS_COMPOSE_ARGS`; runs `uv run pytest tests/integration_peers -m integration -p no:cacheprovider` with `CCW_INTEGRATION_TASK=1`; prints compose logs on failure; always `down -v --remove-orphans`.
- mise.toml: new task `test:integration:peers` running `scripts/integration-peers.sh`; `test:integration` runs `scripts/integration.sh` (which builds `claude-code-wrapper:local`) and then `CCW_SKIP_BUILD=1 scripts/integration-peers.sh`, so CI builds the image once and runs both. tests/test_ci_workflows.py stays green unchanged.

Interfaces: none new.

Notes: the in-process suite proves the logic; this task proves the container boundary: service-DNS seeds and advertise urls, signed requests between containers, the image, and leave on container stop. Real-claude acceptance of the tool schemas is cannotVerify, not part of this task.

## Acceptance conditions

- AC1: two containers join, list each other, exchange messages both ways with one reply each and idle notices, and A drops B through its leaving heartbeat when B stops [evidence: command:bash scripts/integration-peers.sh]
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

See the Requirement's Interfaces paragraph; keep every name and signature stated there.

## Expected outcome

CI runs a two-container peers check after the existing integration check, on an image built once, and it passes.

## Steps

- Write the tests named in the acceptance conditions in `tests/integration_peers/test_peers_containers.py` first; run `bash scripts/integration-peers.sh` and see them fail for the missing behaviour (not an import typo).
- Implement the smallest change that makes them pass; run `bash scripts/integration-peers.sh` again and expect all passed.
- Run the task verify: `bash scripts/integration-peers.sh && mise run lint`; expect all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

See the Requirement's Notes paragraph. The plain pytest run deselects integration tests, so red and green both use the script.
