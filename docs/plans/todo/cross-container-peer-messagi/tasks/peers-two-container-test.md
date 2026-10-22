---
title: Peers two-container test
status: todo
repo: .
value: Proof on the real image that a session in one container finds and reaches a session in another
files: [docker-compose.peers.ci.yml, scripts/integration-peers.sh, mise.toml, tests/integration_peers/__init__.py, tests/integration_peers/conftest.py, tests/integration_peers/test_peers_containers.py, tests/integration_peers/scenario.json, tests/integration_peers/config.ci.yaml]
verify: "mise run test:integration:peers && mise run lint"
depends_on: [manager-peers-lifecycle, peers-image-compose-and-docs]
owns: []
uses: [peers-image, fake-peer-socket]
model: sonnet
estimate: 90
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Peers two-container test

## Requirement

A docker integration test that proves cross-container discovery and socket reachability with the real image and the fake claude: two ccw containers share one peers volume, a session in each, A's `peers` tool lists B, and a connection from container A to B's socket delivers a line (design D8). Real-claude delivery cannot be tested here (no credentials): it is recorded as cannotVerify in the run and the record, not left as an open task.

- docker-compose.peers.ci.yml: an override of examples/docker-compose.peers.yml for both `ccw-a` and `ccw-b`: `CCW_AUTH_TOKEN: ci-token`, empty credentials, `restart: "no"`, mounts of `./tests/fake_claude:/opt/fake-claude:ro`, `./tests/integration/claude-shim.sh:/opt/ci/claude:ro`, `./tests/integration_peers/scenario.json:/opt/ci/scenario.json:ro` and `./tests/integration_peers/config.ci.yaml:/etc/ccw/config.yaml:ro`. Relative paths resolve against the repo root because the script passes `--project-directory .`.
- tests/integration_peers/config.ci.yaml: auth token `ci-token`, `claude.binary: /opt/ci/claude`, `FAKE_CLAUDE_SCENARIO: /opt/ci/scenario.json` in `claude.env.extra`, the boot dialog and ready markers the fake needs (as tests/integration/config.ci.yaml), global mise install off, `peers: {enabled: true, dir: /peers, name: ${CCW_PEERS_NAME:-}}`.
- tests/integration_peers/scenario.json: a turn matching `list peers` whose steps are `{"mcp": {"server": "ccw", "tool": "peers", "arguments": {}}}` then a text step.
- scripts/integration-peers.sh (bash, `set -euo pipefail`, executable): `docker compose -p ccw-peers-it --project-directory . -f examples/docker-compose.peers.yml -f docker-compose.peers.ci.yml up -d --build ccw-a ccw-b`, waits for `/healthz` and `/readyz` on both ports with a bounded deadline (`CCW_HEALTH_TIMEOUT`, default 120 s), exports `CCW_PEERS_A_URL`, `CCW_PEERS_B_URL`, `CCW_PEERS_PROJECT` and `CCW_INTEGRATION_TASK=1`, runs `uv run pytest tests/integration_peers -m integration -p no:cacheprovider`, prints compose logs on failure and always runs `down -v --remove-orphans`. Ports from `CCW_A_PORT`/`CCW_B_PORT` (defaults 8081, 8082).
- mise.toml: task `test:integration:peers` runs scripts/integration-peers.sh; task `test:integration` runs scripts/integration.sh and then scripts/integration-peers.sh, so the CI integration job (`mise run test:integration`) covers it with no workflow change.
- tests/integration_peers/__init__.py and conftest.py: every test is marked `integration` (deselected by the default `-m 'not integration'`); an autouse session fixture checks both `/healthz` and fails when unreachable under `CCW_INTEGRATION_TASK=1`, else marks the tests as not runnable here (same pattern as tests/integration/conftest.py); fixtures `api_a`, `api_b` (httpx clients with the bearer token) and a helper `compose_exec(service, *argv) -> subprocess.CompletedProcess` running `docker compose -p $CCW_PEERS_PROJECT ... exec -T <service> ...`.
- tests/integration_peers/test_peers_containers.py:
  - create a session named `beta` in B and one named `alpha` in A, wait for both ready (bounded);
  - submit `list peers` in A, wait for the turn to complete; A's transcript (`GET /v1/sessions/{id}/transcript`) contains B's session id and `uds:/peers/socks/<B id>.sock` and the peers list does not contain A's own id;
  - from container A, `compose_exec("ccw-a", "python", "-I", "-c", <connect to B's socket path, send one NDJSON user line, close>)` exits 0;
  - `compose_exec("ccw-b", "cat", "/sessions/<B id>/config/fake-peer-inbox.jsonl")` contains that line;
  - both sessions are destroyed in `finally`.

Interfaces: `mise run test:integration:peers`; compose project `ccw-peers-it`.

Notes: the fake binds the socket because ccw passes `--messaging-socket-path` (fake-claude-peer-socket); the claude shim already exists at tests/integration/claude-shim.sh and is reused, not copied. A Python AF_UNIX client in one container reaching a server in another through a named volume was verified on Docker Desktop (fact F9). CLAUDE_CONFIG_DIR is `/sessions/<id>/config` in the image layout.

## Acceptance conditions

- AC1: the two-container stack builds, both services become ready, and the peers integration tests pass [evidence: command:mise run test:integration:peers]
- AC2: A's peers tool output lists B's session id and its uds address under /peers/socks and not A's own id [evidence: judgement]
- AC3: a connection from container A to B's socket delivers one NDJSON line into B's fake inbox [evidence: judgement]
- AC4: mise test:integration runs both the existing integration script and the peers script [evidence: command:grep -q "test:integration:peers" mise.toml && grep -q "integration-peers.sh" mise.toml]
- AC5: the peers integration tests are deselected from the default unit run [evidence: command:uv run pytest tests/integration_peers -q --co 2>&1 | grep -q deselected]
- AC6: lint passes on the new files [evidence: command:mise run lint]

## Files

- `docker-compose.peers.ci.yml`
- `scripts/integration-peers.sh`
- `mise.toml`
- `tests/integration_peers/__init__.py`
- `tests/integration_peers/conftest.py`
- `tests/integration_peers/test_peers_containers.py`
- `tests/integration_peers/scenario.json`
- `tests/integration_peers/config.ci.yaml`

## Interfaces

As in the Requirement's Interfaces paragraph: `mise run test:integration:peers`, compose project `ccw-peers-it`.

## Expected outcome

`mise run test:integration:peers` builds the image, starts two containers on one peers volume, and passes: A lists B through the peers tool and a line sent from container A arrives in B's fake inbox.

## Steps

- Write the tests named in the acceptance conditions in `tests/integration_peers/test_peers_containers.py` first; run `mise run test:integration:peers` (after adding the mise task and script) and see it fail on the missing compose override, CI config or scenario, not on a typo.
- Implement the smallest change that makes them pass; run `mise run test:integration:peers` and expect all passed and the stack torn down.
- Run the task verify: `mise run test:integration:peers && mise run lint`; expect pytest all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

See the Requirement's Notes paragraph. Integration tests are deselected by the default unit run, so the red step for this task is `scripts/integration-peers.sh` failing before the compose override, CI config and scenario exist.
- Builder check: the evidence for judgement ACs is the pytest assertions in tests/integration_peers/test_peers_containers.py, run by AC1's command. If the fake's MCP tool result does not show up in `GET /transcript`, assert on the fake's fake-mcp.jsonl responses through compose_exec instead. The peers script runs in the same CI integration job via `test:integration`; build the image once.
