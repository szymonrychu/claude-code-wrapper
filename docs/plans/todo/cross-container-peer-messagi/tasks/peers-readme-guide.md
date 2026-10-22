---
title: Peers readme guide
status: todo
repo: .
value: "Operators can set up, secure and reason about peer messaging and membership from the README alone"
files: [README.md, tests/test_peers_readme.py]
verify: "uv run pytest tests/test_peers_readme.py tests/test_readme_config.py tests/test_readme_api.py -q && mise run lint"
depends_on: [peer-messages-endpoint, peers-join-leave-order, peers-compose-example]
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

- `## Peer messaging`: what it is and that it replaces Claude's native SendMessage/ListAgents for ccw sessions (and why: Unix sockets do not cross nodes, even on RWX CephFS; a shared folder is not needed); what ccw switches off (`CLAUDE_CODE_HARBOR_KITE=0`, the settings deny rules, the gate reason); setup (enable, shared token, names, advertise urls, seeds); addressing `<instance>/<session id>`; membership: `peers.static` seeds and `ccw serve --peer`, join, heartbeats with gossiped member lists, reachability and `max_missed_heartbeats`, leave, standalone after a preJoin deny, the startup order (preJoin, join, postJoin before any session) and shutdown order (preLeave, leave, postLeave before any onDestroy) and their bounds; instance names must be unique within a mesh; request signing (the three headers, the skew window, nonces); delivery as new turns in arrival order and the frames, reply only when needed; notices (delivered with notify_when_idle, rejected and dropped always); delivery guarantees: at most once, a restart loses the in-memory inbox, the dedupe window of the last 256 `(from, msg_id, kind)` per session, one client retry on connection failures with the same msg_id; limits (`max_message_bytes`, `inbox_max`, `max_messages_per_minute`, body cap, `max_clock_skew_seconds`); cross-node and cross-cluster use (every instance's `advertise_url` reachable from every other, seeds as service DNS names, NTP-synced clocks within `max_clock_skew_seconds`); TLS: ccw signs but does not encrypt and does not terminate TLS, so cross-host traffic needs TLS in front (an ingress or service mesh, `advertise_url` https).
- Trust model, in `## Peer messaging` and as a pointer from `## Trust model`: one shared peer token is one trust domain; any holder can push prompts into bypass-mode agents and claim any `from`; any agent running as the agent uid can read the token from the ccw process env; any holder can join the mesh; main-agent-only is not enforced against a subagent calling `/internal/mcp` from Bash (same as the command tool).
- `### \`POST /internal/mcp\``: the `peers` and `send_message` tools, arguments, results and error prefixes, only with `peers.enabled`; `### The ccw command tool`: the reworded subagent reason.
- The `peer` turn origin wherever origins are listed (`## Overview`, `turn_started` data, `onTurn`).
- The four membership hooks are cross-referenced from `## Peer messaging` to their `## Hooks` entries.
- `## Docker and compose`: the two-instance compose example (examples/docker-compose.peers.yml) with each service seeded by the other. `## Running in Kubernetes`: a per-pod `advertise_url` and a headless Service (or a StatefulSet pod DNS name) as the seed.
- tests/test_peers_readme.py: the `## Peer messaging` section contains `<instance>/<session id>`, `TLS`, `at most once`, `unique`, `X-CCW-Peer-Signature`, `peers.static`, `--peer`, `preJoin`, `postLeave`, `max_missed_heartbeats`; the trust model phrases (`trust domain`, `/internal/mcp`, `process env`); `send_message` and `peers` under `POST /internal/mcp`; the `peer` origin in the turn_started docs; `docker-compose.peers.yml` under `## Docker and compose`; `headless` under `## Running in Kubernetes`.

Interfaces: none (documentation).

## Acceptance conditions

- AC1: the peer messaging section covers setup, seeds, membership order, addressing, unique names, signing, at-most-once delivery, cross-node and TLS [evidence: test:tests/test_peers_readme.py::test_peer_section]
- AC2: the trust model covers the shared trust domain, token readability by the agent uid and the /internal/mcp subagent caveat [evidence: test:tests/test_peers_readme.py::test_trust_model]
- AC3: the MCP tools, the peer turn origin, the compose example and the Kubernetes note are documented [evidence: test:tests/test_peers_readme.py::test_tools_origin_compose_k8s]
- AC4: the README has the Peer messaging section [evidence: doc-section:README.md#Peer messaging]
- AC5: the README config, API and slash command doc suites stay green [evidence: command:uv run pytest tests/test_readme_config.py tests/test_readme_api.py tests/test_slash_commands_docs.py -q]

## Files

- `README.md`
- `tests/test_peers_readme.py`


## Interfaces

See the Requirement's Interfaces paragraph; keep every name and signature stated there.

## Expected outcome

An operator can set up, secure and reason about peer messaging and membership from the README alone.

## Steps

- Write the tests named in the acceptance conditions in `tests/test_peers_readme.py` first; run `uv run pytest tests/test_peers_readme.py -q` and see them fail for the missing behaviour (not an import typo).
- Implement the smallest change that makes them pass; run `uv run pytest tests/test_peers_readme.py -q` again and expect all passed.
- Run the task verify: `uv run pytest tests/test_peers_readme.py tests/test_readme_config.py tests/test_readme_api.py -q && mise run lint`; expect all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

Doc task with a test pinning the content. Last README-editing task (README order: peers-config-section, peer-events-and-metrics, peer-membership-hooks, peer-messages-endpoint, peers-readme-guide.)
