---
title: Cross-container peer messaging over a shared mount
blockers: []
stage: build
owner: ""
repos: [claude-code-wrapper]
created: 2026-10-22
---

# Cross-container peer messaging over a shared mount

## Project brief

What: Native Claude peer messaging between ccw sessions and containers over a shared /peers mount, with a ccw peers discovery tool
How: --messaging-socket-path into a shared 0700 dir, crossSessionInbound accept, ccw-written peer directory plus a peers MCP tool, compose example and docker integration test
Why: The user will run several ccw containers whose main agents must talk to each other

## Description

Main agents in different ccw containers (and different sessions in one container) should be able to message each other with Claude Code's native peer messaging (SendMessage to a `uds:` socket address). Today they cannot: each session has a private CLAUDE_CONFIG_DIR registry, sockets live in a private /tmp, every container's claude gets the same small pid so a shared registry collides, and ccw's bypass permission mode makes Claude hold inbound peer messages for a TUI approval nobody can give.

Research (2.1.304/2.1.307 bundles plus Docker experiments): scratchpad peer-messaging-research.md, carried into design.md. User decision: shared mount plus a ccw `peers` tool (not a shared pid namespace).

## Goals

- Opt-in `peers` config section; off by default, no behaviour change when off.
- When on, each session binds its messaging socket at `<peers.dir>/socks/<session id>.sock` via `--messaging-socket-path`, with `crossSessionInbound: accept`, `CLAUDE_CODE_HARBOR_KITE=1` and `CLAUDE_CODE_SESSION_NAME` set.
- ccw publishes a peer directory on the shared mount and the ccw MCP server exposes a `peers` tool listing live peers (socket probe) with their `uds:` addresses.
- The image pre-creates the peers dir owned by the agent uid with mode 0700, so a fresh named volume works.
- A compose example with two ccw containers sharing one peers volume, and a docker integration test proving a session in one container lists and connects to a session in the other.
- README documents setup, addressing, the same-host limit and the trust model.

## Non-goals

- Shared pid namespace or native ListAgents discovery across containers.
- Mirroring Claude's own session registry.
- Cross-host messaging or an HTTP relay (Unix sockets are kernel-local).
- A public HTTP peers endpoint.

## Time

Estimate: 540 min | Actual: 0 min | Remaining estimate: 540 min

## Review

Filled when the epic reaches stage `review`: the verdict, the findings acted on, the findings dropped.
