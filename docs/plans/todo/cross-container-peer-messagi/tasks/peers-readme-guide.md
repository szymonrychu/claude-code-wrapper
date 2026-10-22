---
title: Peers readme guide
status: todo
repo: .
value: "Operators can set up, secure and reason about peer messaging from the README alone"
files: [README.md, tests/test_peers_readme.py]
verify: "uv run pytest tests/test_peers_readme.py tests/test_readme_config.py tests/test_readme_api.py -q && mise run lint"
depends_on: [peer-messages-endpoint, manager-peers-lifecycle, peers-image-compose-docs]
owns: []
uses: []
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

# Peers readme guide

## Requirement

The user-facing peer messaging documentation (design D11). The last of the four README-editing tasks; it documents the finished behaviour of the whole epic. Files: README.md and tests/test_peers_readme.py only.

- `## Peer messaging`: what it is and that it replaces Claude's native SendMessage/ListAgents for ccw sessions (and why: Unix sockets do not cross nodes, even on RWX CephFS); what ccw switches off (`CLAUDE_CODE_HARBOR_KITE=0`, the settings deny rules, the gate reason); setup (enable, shared token, shared directory, names, advertise urls); addressing `<instance>/<session id>`; instance names must be unique per directory (startup sweeps its own name dir and warns when an entry looked live); the directory layout, entry keys, signatures, heartbeat, staleness and the 10 x stale_seconds garbage collection; request signing (the three headers, the skew window, nonces); delivery as new turns in arrival order and the frames, reply only when needed; notices (delivered with notify_when_idle, rejected and dropped always); delivery guarantees: at most once, a restart loses the in-memory inbox, the dedupe window of the last 256 `(from, msg_id, kind)` per session, one client retry on connection failures with the same msg_id; limits (`max_message_bytes`, `inbox_max`, `max_messages_per_minute`, body cap, `max_clock_skew_seconds`); cross-node and cross-cluster use (any RWX volume such as CephFS, `advertise_url` reachable from every instance, NTP-synced clocks within `max_clock_skew_seconds`); TLS: ccw signs but does not encrypt and does not terminate TLS, so cross-host traffic needs TLS in front (an ingress or service mesh, `advertise_url` https).
- Trust model, in `## Peer messaging` and as a pointer from `## Trust model`: one shared peer token is one trust domain; any holder can push prompts into bypass-mode agents and claim any `from`; any agent running as the agent uid can read the token from the ccw process env; anyone who can write the directory can delete entries (but not forge them without the token); main-agent-only is not enforced against a subagent calling `/internal/mcp` from Bash (same as the command tool).
- `### \`POST /internal/mcp\``: the `peers` and `send_message` tools, arguments, results and error prefixes, only with `peers.enabled`; `### The ccw command tool`: the reworded subagent reason.
- The `peer` turn origin wherever origins are listed (`## Overview`, `turn_started` data, `onTurn`).
- `## Docker and compose`: the two-instance compose example (examples/docker-compose.peers.yml) and the pre-created `/peers`. `## Running in Kubernetes`: an RWX PVC for `/peers` across nodes and a per-pod `advertise_url` (for example a headless Service DNS name).
- tests/test_peers_readme.py: the `## Peer messaging` section contains `<instance>/<session id>`, `RWX`, `TLS`, `at most once`, `unique`, `X-CCW-Peer-Signature`; the trust model phrases (`trust domain`, `/internal/mcp`, `process env`); `send_message` and `peers` under `POST /internal/mcp`; the `peer` origin in the turn_started docs; `docker-compose.peers.yml` under `## Docker and compose`; `RWX` under `## Running in Kubernetes`.

Interfaces: none (documentation).

## Acceptance conditions

- AC1: the peer messaging section covers setup, addressing, unique names, signing, at-most-once delivery, cross-node and TLS [evidence: test:tests/test_peers_readme.py::test_peer_section]
- AC2: the trust model covers the shared trust domain, token readability by the agent uid and the /internal/mcp subagent caveat [evidence: test:tests/test_peers_readme.py::test_trust_model]
- AC3: the MCP tools, the peer turn origin, the compose example and the Kubernetes note are documented [evidence: test:tests/test_peers_readme.py::test_tools_origin_compose_k8s]
- AC4: the README has the Peer messaging section [evidence: doc-section:README.md#Peer messaging]
- AC5: the README config, API and slash command doc suites stay green [evidence: command:uv run pytest tests/test_readme_config.py tests/test_readme_api.py tests/test_slash_commands_docs.py -q]

## Files

- `README.md`
- `tests/test_peers_readme.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
