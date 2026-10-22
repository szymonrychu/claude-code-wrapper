---
title: Peers image compose and docs
status: todo
repo: .
value: Users can run two peered ccw containers from one compose file and know the limits and trust model
files: [Dockerfile, examples/docker-compose.peers.yml, src/claude_code_wrapper/data/examples/peers.config.yaml, examples/peers.config.yaml, README.md, tests/test_peers_container.py]
verify: "uv run pytest tests/test_peers_container.py tests/test_container.py tests/test_examples.py -q && mise run lint"
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

# Peers image compose and docs

## Requirement

The image, a runnable two-container compose example and the README peer messaging docs (design D7, D9; facts F7, F9). Owns part `peers-image` (Dockerfile `/peers`, examples/docker-compose.peers.yml, the peers example config).

- Dockerfile: the user-creation RUN (Dockerfile "uid/gid 1000" block) also creates `/peers`, chowns it to `agent:agent` and sets mode 0700, so a fresh named volume mounted at `/peers` inherits uid 1000 and 0700 (a fresh named volume is otherwise root:root 755 and uid 1000 gets EACCES, fact F9).
- src/claude_code_wrapper/data/examples/peers.config.yaml: a minimal config with `peers: {enabled: true, dir: /peers, name: ${CCW_PEERS_NAME:-}}`, `server.auth_token: ${CCW_AUTH_TOKEN:-}` style interpolation as the other examples do, and comments explaining each peers key; it loads with the stub env of tests/test_examples.py.
- examples/peers.config.yaml: a committed symlink to `../src/claude_code_wrapper/data/examples/peers.config.yaml`, like the other examples (git mode 120000).
- examples/docker-compose.peers.yml: a standalone compose file with services `ccw-a` and `ccw-b`, both `build: {context: .., dockerfile: Dockerfile}` and `image: claude-code-wrapper:local`, `hostname` ccw-a/ccw-b, `CCW_PEERS_NAME` a/b, `CCW_AUTH_TOKEN: ${CCW_AUTH_TOKEN:?set CCW_AUTH_TOKEN}`, credentials passed as in docker-compose.yml, ports `127.0.0.1:${CCW_A_PORT:-8081}:8080` and `127.0.0.1:${CCW_B_PORT:-8082}:8080`, their own workdir, sessions and mise volumes, the shared named volume `peers` mounted at `/peers` in both, the config mounted from `./peers.config.yaml` read-only, and `stop_grace_period: 30s`. Top-level volumes declare `peers` plus the per-service ones.
- README:
  - a new `## Peer messaging` section: setup (enable `peers`, run the compose example with the exact command, the image's pre-created `/peers`, a bind mount must be owned by uid 1000 with mode 0700), addressing (sockets at `<peers.dir>/socks/<session id>.sock`, address `uds:<path>`, the ccw `peers` tool, SendMessage, `CLAUDE_CODE_SESSION_NAME` = `<instance>-<session name or id>`), what ccw sets per session (`--messaging-socket-path`, `CLAUDE_CODE_HARBOR_KITE=1`, `CLAUDE_CODE_SESSION_NAME`, `crossSessionInbound: accept`), the same-host-only limit (Unix sockets are kernel-local; Kubernetes needs the pods on one node sharing a hostPath or RWX local volume at the same path and the same uid; no cross-host messaging), and the trust model (anything running as the agent uid with access to the dir can inject prompts into bypass-mode agents; the auth line is optional on Linux; `crossSessionInbound: accept` turns off Claude's parity hold; replies, receipts and idle notices work because every socket shares one directory).
  - `## Trust model` gets one paragraph pointing to it; `## Docker and compose` names examples/docker-compose.peers.yml.
- tests/test_peers_container.py: static checks of all of the above (Dockerfile text, compose YAML structure, example config load, `git ls-files -s examples/peers.config.yaml` mode 120000 and target, README section content).

Interfaces: image path `/peers` (agent:agent 0700); compose services `ccw-a`, `ccw-b`, volume `peers`; env `CCW_PEERS_NAME`, `CCW_A_PORT`, `CCW_B_PORT`.

Notes: tests/test_examples.py already loads every examples/*.yaml, so the compose file must use the `.yml` extension. tests/test_container.py pins the existing Dockerfile shape (non-root, pins with reasons); keep it green. Check tests/test_packaging.py `test_wheel_contains_data_files` if it enumerates example data files. Plain ASCII, no em dashes.

## Acceptance conditions

- AC1: the Dockerfile creates /peers owned by agent with mode 0700 [evidence: test:tests/test_peers_container.py::test_dockerfile_precreates_peers_dir]
- AC2: the compose example runs two services sharing the named peers volume at /peers with separate state volumes and loopback ports [evidence: test:tests/test_peers_container.py::test_compose_example_shares_peers_volume]
- AC3: the peers example config loads with peers enabled at /peers and is a committed symlink into the package data [evidence: test:tests/test_peers_container.py::test_peers_example_config_symlinked_and_loads]
- AC4: README Peer messaging covers setup, uds addressing, the same-host limit with the Kubernetes note, and the trust model [evidence: test:tests/test_peers_container.py::test_readme_peer_messaging_section]
- AC5: README has the Peer messaging heading [evidence: doc-section:README.md#Peer messaging]
- AC6: existing container, example and packaging checks stay green [evidence: command:uv run pytest tests/test_container.py tests/test_examples.py tests/test_packaging.py tests/test_readme_config.py -q]

## Files

- `Dockerfile`
- `examples/docker-compose.peers.yml`
- `src/claude_code_wrapper/data/examples/peers.config.yaml`
- `examples/peers.config.yaml`
- `README.md`
- `tests/test_peers_container.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
