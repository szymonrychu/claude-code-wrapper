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

What: ccw-owned peer messaging between the main agents of ccw sessions, across containers, nodes and clusters, replacing Claude's native SendMessage/ListAgents for ccw sessions
How: an opt-in `peers:` config section, a heartbeat-refreshed JSON peer directory on a shared (optionally RWX) volume, two ccw MCP tools (`peers`, `send_message`), an HMAC-signed `POST /v1/peers/messages` and `POST /v1/peers/notices`, and a per-session inbox that delivers each message as a new turn when the session is idle
Why: the user runs several ccw containers, possibly on different nodes, whose main agents must talk to each other; Claude's native peer messaging rides on Unix sockets, which never cross a node, not even on RWX CephFS

## Description

Main agents in different ccw sessions (same container, other containers, other nodes or clusters) should message each other. The first plan used Claude Code's native peer messaging (`SendMessage` to a `uds:` socket on a shared mount). The user rejected it: Unix sockets are kernel-local and do not cross nodes, even on a shared RWX CephFS volume. User decision: "go with custom tooling".

ccw now owns the whole path. Discovery is a directory of small JSON files on a shared volume, one per live session, refreshed by a heartbeat. The agent lists peers and sends messages with two ccw MCP tools. The sending ccw POSTs the message to the target instance's public HTTP API, signed with HMAC-SHA256 keyed by a shared peer token that never travels on the wire; directory entries are signed the same way. The target ccw queues it in the session's inbox and delivers it as a new turn when the session is idle, framed as an `<agent-message>` that tells the agent how to reply. With `notify_when_idle` the target posts an idle notice back to the sender once the turn that handled the message ends. Claude's native SendMessage and ListAgents are switched off for peered sessions so the agent never reaches for a transport that cannot work.

The research brief on native messaging (2.1.304/2.1.307 bundles plus Docker experiments) is background only; its findings and the reason the native design was dropped are carried in design.md.

## Goals

- Opt-in `peers:` config section (enabled, name, advertise_url, token, directory, heartbeat_seconds, stale_seconds, max_message_bytes, inbox_max, max_clock_skew_seconds, max_messages_per_minute); off by default and zero behaviour change when off.
- When on, the native SendMessage and ListAgents are disabled three ways (env, settings deny, tool gate) and the agent is told to use `mcp__ccw__peers` and `mcp__ccw__send_message`.
- A heartbeat-refreshed peer directory on a shared volume, readable by every ccw instance that mounts it, with liveness by heartbeat age.
- `POST /v1/peers/messages` and `POST /v1/peers/notices`, authenticated by HMAC request signatures with a clock-skew window and nonce replay protection, with size, inbox and per-sender rate bounds.
- Per-session inbox delivery as new turns, in arrival order, never into a busy PTY; replies and `notify_when_idle` idle notices work end to end; a rejected or dropped message always produces a notice to its sender; delivery is at most once.
- SSE events and Prometheus counters for received, sent and delivered peer messages.
- Proof: unit tests per module, an in-process test with two ccw services on real HTTP ports, and a docker two-container test run by `mise run test:integration` in CI.
- Image, compose example and README (setup, addressing, config rows, routes, trust model, cross-node, TLS note).

## Non-goals

- Native Claude peer messaging over Unix sockets, `--messaging-socket-path`, `crossSessionInbound`, a shared pid namespace or mirroring Claude's session registry.
- A static peer list or a ccw-to-ccw listing API; discovery is the shared directory only.
- TLS termination in ccw; `advertise_url` may be https behind an ingress or mesh.
- Messaging subagents or teammates; the tools are main-agent only.
- Testing the tool schemas against a real claude (no credentials); recorded as cannotVerify.

## Time

Estimate: 1125 min | Actual: 0 min | Remaining estimate: 1125 min

## Review

Filled when the epic reaches stage `review`: the verdict, the findings acted on, the findings dropped.
