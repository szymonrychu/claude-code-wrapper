---
title: Cross-container peer messaging over a shared mount
blockers: []
stage: review
owner: Szymon Richert
repos: [claude-code-wrapper]
created: 2026-10-22
---

# Cross-container peer messaging over a shared mount

## Project brief

What: ccw-owned peer messaging between the main agents of ccw sessions, across containers, nodes and clusters, replacing Claude's native SendMessage/ListAgents for ccw sessions
How: an opt-in `peers:` config section; instance membership through one signed heartbeat endpoint (first round = join, a `leaving: true` heartbeat = leave) with a static seed list (`peers.static`, `ccw serve --peer`) and preJoin/postJoin/preLeave/postLeave hooks; two ccw MCP tools (`peers`, `send_message`); HMAC-signed message and notice endpoints; a per-session inbox that delivers each message as a new turn when the session is idle
Why: the user runs several ccw containers, possibly on different nodes, whose main agents must talk to each other; Claude's native peer messaging rides on Unix sockets, which never cross a node

## Description

Main agents in different ccw sessions (same container, other containers, other nodes or clusters) should message each other. The first plan used Claude Code's native peer messaging over Unix sockets on a shared mount; the user rejected it because sockets do not cross nodes, even on RWX CephFS ("go with custom tooling"). The second plan discovered peers through a shared folder; the user rejected that too: "No.. wrapper supports join/leave endpoints. Config/arguments allow to provide static list of peers for discovery. Leave/join happens on container leave/join. Join operation precedes start hook, leave precedes last hook in lifecycle. New hooks exist pre/postJoin and pre/postLeave. Heartbeat keeps reachable agents list up to date."

So each ccw instance joins the mesh at startup by heartbeating its seeds (preJoin, first heartbeat round, postJoin) before any session can start, learns the other members from the answers, keeps the reachable list current with signed heartbeats, and leaves on shutdown with a final `leaving: true` heartbeat (preLeave, leaving heartbeat, postLeave) before any session's onDestroy. The agent lists peer sessions and sends messages with two ccw MCP tools. Every peer request is signed with HMAC-SHA256 keyed by a shared peer token that never travels on the wire. The target ccw queues a message in the session's inbox and delivers it as a new turn when the session is idle, framed as an `<agent-message>`; notices report delivery (with `notify_when_idle`) and always report rejected or dropped messages. Claude's native SendMessage and ListAgents are switched off for peered sessions.

The research brief on native messaging is background only; its findings and the reasons both earlier designs were dropped are carried in design.md.

## Goals

- Opt-in `peers:` config section (enabled, name, advertise_url, token, static, heartbeat_seconds, max_missed_heartbeats, join_timeout_seconds, max_message_bytes, inbox_max, max_clock_skew_seconds, max_messages_per_minute) plus `ccw serve --peer URL`; off by default and zero behaviour change when off.
- One heartbeat endpoint and no join or leave endpoint (the user: "Leave/join might be redundant with the static list, no? If yes keep heartbeat only and drop the other 2 endpoints"): the first heartbeat round is the join at container start before any session's onCreate, a final `leaving: true` heartbeat is the leave at container stop before any onDestroy, both bounded; one reachable seed is enough to converge.
- New instance-level hooks preJoin (may veto: the instance runs standalone), postJoin, preLeave and postLeave.
- Heartbeats keep the reachable member list up to date: missed heartbeats drop a member, a leaving heartbeat drops it at once, a returning member is re-added.
- When on, the native SendMessage and ListAgents are disabled three ways (env, settings deny, tool gate) and the agent is told to use `mcp__ccw__peers` and `mcp__ccw__send_message`.
- Every peer route authenticated by HMAC request signatures with a clock-skew window and nonce replay protection, with size, inbox and per-sender rate bounds.
- Per-session inbox delivery as new turns, in arrival order, never into a busy PTY; replies and notices work end to end; delivery is at most once.
- SSE events and Prometheus metrics for messages and membership.
- Proof: unit tests per module, an in-process test with three ccw services on real ports (seed join and gossip), and a docker two-container test run by `mise run test:integration` in CI.
- Compose example and README (setup, seeds, membership order, addressing, config rows, routes, hooks, trust model, cross-node, TLS note).

## Non-goals

- Native Claude peer messaging over Unix sockets, `--messaging-socket-path`, `crossSessionInbound`, a shared pid namespace or mirroring Claude's session registry.
- Discovery through a shared folder or volume, and the Dockerfile `/peers` directory.
- Strongly consistent membership or a consensus protocol; the view converges by heartbeat gossip.
- TLS termination or encryption in ccw; `advertise_url` may be https behind an ingress or mesh.
- Messaging subagents or teammates; the tools are main-agent only.
- Testing the tool schemas against a real claude (no credentials); recorded as cannotVerify.

## Time

Estimate: 1395 min | Actual: 0 min | Remaining estimate: 0 min

## Review

Filled when the epic reaches stage `review`: the verdict, the findings acted on, the findings dropped.
