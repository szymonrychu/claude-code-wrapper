---
title: Peers compose example
status: todo
repo: .
value: Operators can run two peered containers seeded with each other from one example and one image build
files: [examples/docker-compose.peers.yml, examples/peers.config.yaml, src/claude_code_wrapper/data/examples/peers.config.yaml, tests/test_examples.py, tests/test_peers_compose.py]
verify: "uv run pytest tests/test_peers_compose.py tests/test_examples.py -q && mise run lint"
depends_on: [peers-config-section]
owns: [peers-compose]
uses: [peers-config]
model: sonnet
estimate: 45
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Peers compose example

## Requirement

Example config and two-instance compose example with static seeds (design D10). No README edits (peers-readme-guide documents them), no Dockerfile change, no shared volume.

- src/claude_code_wrapper/data/examples/peers.config.yaml: a working peers config (real claude): `server.auth_token: ${CCW_AUTH_TOKEN}`, `peers: {enabled: true, name: ${CCW_PEER_NAME}, advertise_url: ${CCW_PEER_URL}, token: ${CCW_PEER_TOKEN}, static: ["${CCW_PEER_SEED}"]}`, commented. examples/peers.config.yaml is a symlink to it created with `ln -s` and `git add`ed so git records mode 120000, like the other examples.
- examples/docker-compose.peers.yml: services `ccw-a` and `ccw-b` built once (`build: ..` and `image: claude-code-wrapper:local` on `ccw-a`, `image: claude-code-wrapper:local` with `pull_policy: never` on `ccw-b`), each with its own `workdir`, `sessions` and `mise` named volumes and loopback port (`127.0.0.1:${CCW_PEER_A_PORT:-8181}:8080`, `127.0.0.1:${CCW_PEER_B_PORT:-8182}:8080`), both mounting `./peers.config.yaml` at `/etc/ccw/config.yaml:ro` (host paths relative to examples/, where Compose resolves them), with `CCW_AUTH_TOKEN: ${CCW_AUTH_TOKEN:?set CCW_AUTH_TOKEN}`, `CCW_PEER_TOKEN: ${CCW_PEER_TOKEN:?set CCW_PEER_TOKEN}`, `CCW_PEER_NAME` `a` / `b`, `CCW_PEER_URL` `http://ccw-a:8080` / `http://ccw-b:8080` and `CCW_PEER_SEED` pointing at the other service (`http://ccw-b:8080` / `http://ccw-a:8080`). No volume is shared between the two services.
- tests/test_examples.py: `STUB_ENV` gains `CCW_AUTH_TOKEN`, `CCW_PEER_TOKEN` (16+ chars, distinct), `CCW_PEER_NAME`, `CCW_PEER_URL` and `CCW_PEER_SEED` so `test_every_example_config_loads` loads the new example.
- tests/test_peers_compose.py: the compose file parses (yaml) with two services, distinct ports, names a and b, each seeded with the other's url, the token from `CCW_PEER_TOKEN`, exactly one `build: ..`, and no named volume used by both services; `git ls-files -s examples/peers.config.yaml` shows mode 120000 and the link target is inside src/claude_code_wrapper/data/examples; the example loads with peers enabled and one static seed.

Interfaces: examples/docker-compose.peers.yml and examples/peers.config.yaml (the `peers-compose` part, consumed by the two-container test).

## Acceptance conditions

- AC1: the compose example builds once and runs two services seeded with each other, distinct ports, names a and b, the shared token and no shared volume [evidence: test:tests/test_peers_compose.py::test_compose_example_two_instances]
- AC2: examples/peers.config.yaml is a committed git symlink into the package data and loads with peers enabled and one seed [evidence: test:tests/test_peers_compose.py::test_example_config_symlink]
- AC3: every example config including the peers one loads [evidence: command:uv run pytest tests/test_examples.py -q]

## Files

- `examples/docker-compose.peers.yml`
- `examples/peers.config.yaml`
- `src/claude_code_wrapper/data/examples/peers.config.yaml`
- `tests/test_examples.py`
- `tests/test_peers_compose.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
