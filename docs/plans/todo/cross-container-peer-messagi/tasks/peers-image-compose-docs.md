---
title: Peers image compose docs
status: todo
repo: .
value: "Operators can run two peered containers from one example and read how peering, trust and cross-node use work"
files: [Dockerfile, examples/docker-compose.peers.yml, examples/peers.config.yaml, src/claude_code_wrapper/data/examples/peers.config.yaml, tests/test_examples.py, tests/test_peers_docs.py, README.md]
verify: "uv run pytest tests/test_peers_docs.py tests/test_examples.py tests/test_container.py -q && mise run lint"
depends_on: [peers-config-section]
owns: [peers-image]
uses: [peers-config]
model: sonnet
estimate: 60
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

Image, compose example and the user-facing peer messaging docs (design D10, D11).

- Dockerfile: `/peers` is created with the other state dirs and owned by `agent:agent` (uid 1000), so Docker copies that ownership into a fresh named volume mounted there.
- src/claude_code_wrapper/data/examples/peers.config.yaml: a working peers config (fake-free, real claude): `server.auth_token: ${CCW_AUTH_TOKEN}`, `peers: {enabled: true, name: ${CCW_PEER_NAME}, advertise_url: ${CCW_PEER_URL}, token: ${CCW_PEER_TOKEN}, directory: /peers}`, commented. examples/peers.config.yaml is a committed symlink to it (git mode 120000), like the other examples.
- examples/docker-compose.peers.yml: services `ccw-a` and `ccw-b` built once (`build: ..` on one, `image: claude-code-wrapper:local` on both, `pull_policy: never` on the second is fine), each with its own `workdir`, `sessions` and `mise` named volumes and its own loopback port (`127.0.0.1:${CCW_PEER_A_PORT:-8181}:8080`, `127.0.0.1:${CCW_PEER_B_PORT:-8182}:8080`), both mounting one named `peers` volume at `/peers` and the peers example config, both with `CCW_PEER_TOKEN: ${CCW_PEER_TOKEN:?set CCW_PEER_TOKEN}`, `CCW_PEER_NAME` `a` / `b` and `CCW_PEER_URL` `http://ccw-a:8080` / `http://ccw-b:8080`.
- tests/test_examples.py: `STUB_ENV` gains `CCW_AUTH_TOKEN`, `CCW_PEER_TOKEN` (16+ chars, distinct), `CCW_PEER_NAME` and `CCW_PEER_URL` so `test_every_example_config_loads` loads the new example.
- README.md:
  - `## Peer messaging`: what it is and that it replaces Claude's native SendMessage/ListAgents for ccw sessions (and why: Unix sockets do not cross nodes, even on RWX CephFS); setup (enable, shared token, shared directory, names, advertise urls); addressing `<instance>/<session id>`; the directory layout, entry keys, heartbeat and staleness; delivery as new turns in arrival order and the two frames; replies and `notify_when_idle`; limits (`max_message_bytes`, `inbox_max`, body cap); cross-node and cross-cluster use (any RWX volume such as CephFS for the directory, `advertise_url` reachable from every instance, NTP-synced clocks within `stale_seconds`); TLS: ccw does not terminate TLS, `advertise_url` may be https behind an ingress or service mesh; trust model: any holder of the peer token can push prompts into bypass-mode agents, so treat it like `server.auth_token`, and anyone who can write the directory can redirect addresses.
  - `## Trust model`: a short pointer to the peer messaging trust model.
  - `## Docker and compose`: the two-instance compose example and the pre-created `/peers`.
  - `## Running in Kubernetes`: one paragraph on an RWX PVC for `/peers` across nodes and a per-pod `advertise_url` (for example a headless Service DNS name).
- tests/test_peers_docs.py: the README has `## Peer messaging` with the setup, addressing, trust model, cross-node and TLS content (assert on key phrases: `<instance>/<session id>`, `RWX`, `TLS`, `peer token`); the Dockerfile creates and chowns `/peers`; the compose file parses (yaml) with two services sharing the `peers` volume at `/peers`, distinct ports, names a and b, and the token from `CCW_PEER_TOKEN`; `git ls-files -s examples/peers.config.yaml` shows mode 120000 pointing into src/claude_code_wrapper/data/examples.

Interfaces: `/peers` in the image (the `peers-image` part), examples/docker-compose.peers.yml and examples/peers.config.yaml (consumed by the two-container test).

## Acceptance conditions

- AC1: the Dockerfile creates /peers owned by agent [evidence: test:tests/test_peers_docs.py::test_dockerfile_creates_peers_dir]
- AC2: the compose example runs two services sharing one peers volume with distinct ports, names a and b and the shared token [evidence: test:tests/test_peers_docs.py::test_compose_example_two_instances]
- AC3: examples/peers.config.yaml is a committed symlink into the package data and loads [evidence: test:tests/test_peers_docs.py::test_example_config_symlink]
- AC4: the README peer messaging section covers setup, addressing, trust model, cross-node and TLS [evidence: test:tests/test_peers_docs.py::test_readme_peer_section]
- AC5: the README has the Peer messaging section [evidence: doc-section:README.md#Peer messaging]
- AC6: every example config including the peers one loads and the container and README suites stay green [evidence: command:uv run pytest tests/test_examples.py tests/test_container.py tests/test_readme_config.py tests/test_readme_api.py -q]

## Files

- `Dockerfile`
- `examples/docker-compose.peers.yml`
- `examples/peers.config.yaml`
- `src/claude_code_wrapper/data/examples/peers.config.yaml`
- `tests/test_examples.py`
- `tests/test_peers_docs.py`
- `README.md`

## Interfaces

See the Requirement's Interfaces paragraph; keep every name and signature stated there.

## Expected outcome

The image ships a writable /peers mount point, a two-instance compose example runs from one build, and the README explains peering end to end.

## Steps

- Write the tests named in the acceptance conditions in `tests/test_peers_docs.py` first; run `uv run pytest tests/test_peers_docs.py -q` and see them fail for the missing behaviour (not an import typo).
- Implement the smallest change that makes them pass; run `uv run pytest tests/test_peers_docs.py -q` and expect all passed.
- Run the task verify: `uv run pytest tests/test_peers_docs.py tests/test_examples.py tests/test_container.py -q && mise run lint`; expect all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

Create the example symlink with ln -s and commit it as a symlink (git mode 120000), like the other examples.
