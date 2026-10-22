---
title: Peers image compose docs
status: todo
repo: .
value: Operators can run two peered containers from one example and one image build
files: [Dockerfile, examples/docker-compose.peers.yml, examples/peers.config.yaml, src/claude_code_wrapper/data/examples/peers.config.yaml, tests/test_examples.py, tests/test_peers_image.py]
verify: "uv run pytest tests/test_peers_image.py tests/test_examples.py tests/test_container.py -q && mise run lint"
depends_on: [peers-config-section]
owns: [peers-image]
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

# Peers image compose docs

## Requirement

Image, example config and two-instance compose example (design D10). No README edits (peers-readme-guide documents them).

- Dockerfile: `/peers` is created with the other state dirs and owned by `agent:agent` (uid 1000), so Docker copies that ownership into a fresh named volume mounted there.
- src/claude_code_wrapper/data/examples/peers.config.yaml: a working peers config (real claude): `server.auth_token: ${CCW_AUTH_TOKEN}`, `peers: {enabled: true, name: ${CCW_PEER_NAME}, advertise_url: ${CCW_PEER_URL}, token: ${CCW_PEER_TOKEN}, directory: /peers}`, commented. examples/peers.config.yaml is a symlink to it created with `ln -s` and `git add`ed so git records mode 120000, like the other examples.
- examples/docker-compose.peers.yml: services `ccw-a` and `ccw-b` built once (`build: ..` and `image: claude-code-wrapper:local` on `ccw-a`, `image: claude-code-wrapper:local` with `pull_policy: never` on `ccw-b`), each with its own `workdir`, `sessions` and `mise` named volumes and its own loopback port (`127.0.0.1:${CCW_PEER_A_PORT:-8181}:8080`, `127.0.0.1:${CCW_PEER_B_PORT:-8182}:8080`), both mounting one named `peers` volume at `/peers` and `./peers.config.yaml` at `/etc/ccw/config.yaml:ro` (host paths relative to examples/, where Compose resolves them), both with `CCW_AUTH_TOKEN: ${CCW_AUTH_TOKEN:?set CCW_AUTH_TOKEN}`, `CCW_PEER_TOKEN: ${CCW_PEER_TOKEN:?set CCW_PEER_TOKEN}`, `CCW_PEER_NAME` `a` / `b` and `CCW_PEER_URL` `http://ccw-a:8080` / `http://ccw-b:8080`.
- tests/test_examples.py: `STUB_ENV` gains `CCW_AUTH_TOKEN`, `CCW_PEER_TOKEN` (16+ chars, distinct), `CCW_PEER_NAME` and `CCW_PEER_URL` so `test_every_example_config_loads` loads the new example.
- tests/test_peers_image.py: the Dockerfile creates and chowns `/peers`; the compose file parses (yaml) with two services sharing the `peers` volume at `/peers`, distinct ports, names a and b, the token from `CCW_PEER_TOKEN` and exactly one `build: ..`; `git ls-files -s examples/peers.config.yaml` shows mode 120000 and the link target is inside src/claude_code_wrapper/data/examples; the example loads with peers enabled.

Interfaces: `/peers` in the image (the `peers-image` part), examples/docker-compose.peers.yml and examples/peers.config.yaml (consumed by the two-container test).

## Acceptance conditions

- AC1: the Dockerfile creates /peers owned by agent [evidence: test:tests/test_peers_image.py::test_dockerfile_creates_peers_dir]
- AC2: the compose example builds once and runs two services sharing one peers volume with distinct ports, names a and b and the shared token [evidence: test:tests/test_peers_image.py::test_compose_example_two_instances]
- AC3: examples/peers.config.yaml is a committed git symlink into the package data and loads with peers enabled [evidence: test:tests/test_peers_image.py::test_example_config_symlink]
- AC4: every example config including the peers one loads and the container suite stays green [evidence: command:uv run pytest tests/test_examples.py tests/test_container.py -q]

## Files

- `Dockerfile`
- `examples/docker-compose.peers.yml`
- `examples/peers.config.yaml`
- `src/claude_code_wrapper/data/examples/peers.config.yaml`
- `tests/test_examples.py`
- `tests/test_peers_image.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
