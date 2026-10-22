# Cross-container peer messaging over a shared mount design

## Summary

ccw gets its own peer messaging for the main agents of its sessions, working across containers,
nodes and clusters. An opt-in `peers:` section (off by default, zero change when off) turns it on.
Discovery is membership between ccw instances over one signed route, `POST /v1/peers/heartbeat`
(D16): each instance joins the mesh with a first heartbeat round to a static seed list
(`peers.static`, extendable with `ccw serve --peer URL`), learns the other members from the
answers, keeps the reachable list up to date with further heartbeats, and leaves with a final
`leaving: true` heartbeat. There is no join or leave endpoint. Join runs before any session
exists; leave runs before any session's onDestroy. New instance-level hooks preJoin, postJoin,
preLeave and postLeave frame both. The ccw MCP server gains `peers` and `send_message`; a send is
an HMAC-signed POST to the target instance (the shared peer token is the signing key and never
travels on the wire); the target delivers it as a new turn when the session is idle and notices
flow back. Claude's native SendMessage/ListAgents are switched off for peered sessions. The
heading keeps the epic's original title; there is no shared mount any more.

## Intent and why

The user runs several ccw containers, possibly on different Kubernetes nodes, whose main agents
must talk to each other. The first plan used Claude Code's native peer messaging over Unix sockets
on a shared mount; the user rejected it because a Unix socket never crosses a node, even on RWX
CephFS. User decision: "go with custom tooling". The second plan used a shared folder of
heartbeat files for discovery; the user rejected that too and specified membership over the
wrapper's HTTP API (D5, verbatim), then judged join and leave endpoints redundant with the static
list and kept the heartbeat only (D16, verbatim).

## Goals

- `peers.enabled: false` (the default): argv, env, settings.json, .claude.json, CLAUDE.md, the
  mounted routes, the hook events that fire, the MCP tool list and outbound traffic are exactly as
  today.
- `peers.enabled: true`: on startup the instance runs preJoin, heartbeats every seed and known
  member, and runs postJoin, before any session can be created; on shutdown (SIGTERM, container
  stop, the exit-when-last-destroyed policy) it runs preLeave, sends a `leaving: true` heartbeat to
  every known member and runs postLeave, before any session's onDestroy.
- One reachable seed is enough: heartbeat answers carry the member list, so the mesh converges.
- Heartbeats keep the reachable list current: a member missing `max_missed_heartbeats`
  consecutive heartbeats is dropped, a leaving heartbeat drops it at once, and a dropped member
  that heartbeats again is re-added.
- `mcp__ccw__peers` lists the sessions of reachable members other than the caller;
  `mcp__ccw__send_message` delivers to a peer session or returns a clear error.
- A delivered message is a new turn on the target session, in arrival order, never typed into a
  busy PTY, and no queued item is ever stranded; a reply and the notices reach the sender the same
  way. A sender always learns when its message was rejected or dropped.
- SendMessage and ListAgents are unavailable to peered sessions (env, settings deny, tool gate).
- The peer token is never sent on the wire, never logged and never written to a file; every peer
  request is signed, time-bounded and replay-protected.
- Membership and heartbeat I/O never blocks session work; an unreachable seed never blocks startup
  beyond `join_timeout_seconds`.
- Events and Prometheus metrics for messages and membership exist and are documented.
- Proof: unit tests per module; an in-process test with three ccw services on real ports (one seed
  each) proving join, gossip, listing, messages both ways, notices and leave; a docker two-container
  test run by `mise run test:integration` in CI.
- README documents setup, seeds, addressing, every key, every route, the four hooks, the trust
  model, delivery guarantees, cross-node use and the TLS note; a compose example runs two peered
  containers from one build.

## Non-goals

- Native Claude peer messaging (Unix sockets, `--messaging-socket-path`, `crossSessionInbound`,
  shared pid namespace, Claude's session registry).
- Discovery through a shared folder or volume.
- A consensus protocol or strongly consistent membership; the view converges by gossip.
- TLS termination or payload encryption inside ccw (ccw signs, it does not encrypt).
- Messaging from subagents or teammates.
- At-least-once delivery or a persistent inbox; delivery is at most once.
- Testing the tool schemas against a real logged-in claude.

## Anti-goals

- Any behaviour change for configs without `peers.enabled: true`.
- A session created before the join finished, or an onDestroy that runs before the leave.
- Startup or shutdown held up without bound by an unreachable member.
- A message typed into a running turn, two peer turns interleaved, or a queued item left waiting
  while the session is idle.
- The peer token on the wire, in a log line, an event, `GET /v1/config` or a file.
- A peer request accepted with a bad signature, a stale timestamp or a replayed nonce; a
  `server.auth_token` bearer opening a peer route, or the peer token opening any other route.
- A Stop hook answer held up by a notice POST to a slow or dead sender.
- Two agents replying to each other forever without a bound.
- An unbounded inbox, nonce cache, member table or request body.

## Constraints

- Repo CLAUDE.md: test-first (red, green, refactor), always `uv run`, plain ASCII, no em dashes,
  never write service files into a session workdir.
- Tests use the fake claude under tests/fake_claude only; only the fake-claude task
  (fake-claude-peer-harness) edits tests/conftest.py.
- Every config key needs a README row (tests/test_readme_config.py); every route, event type and
  hook event needs a README heading (tests/test_readme_api.py); every API error code is
  documented under `## Errors`; every HOOK_EVENTS name appears in config.example.yaml
  (tests/test_config.py `test_full_example_loads`).
- README.md is edited by exactly five tasks, serialized through dependsOn: peers-config-section,
  peer-events-and-metrics, peer-membership-hooks, peer-messages-endpoint, peers-readme-guide.
- examples/*.config.yaml are committed symlinks into src/claude_code_wrapper/data/examples.
- The EVENT_TYPES pin in tests/test_event_log.py is updated, not loosened.
- Tasks of about an hour, at most 8 files.

## Facts (background: native messaging research, carried from the scratch brief)

Source: reverse engineering of the claude 2.1.307 (macOS) and 2.1.304 (Linux) bundles plus
throwaway Docker experiments, no credentials. Kept: F8 (switching the built-ins off), F7 (reply
and idle-notice semantics), F10 (ccw today).

- F1 Registry: native discovery is `$CLAUDE_CONFIG_DIR/sessions/<pid>.json`, keyed by pid with no
  uniqueness check.
- F5 Pid collision: two containers sharing one registry both ran claude as pid 10 and one record
  overwrote the other.
- F7 Replies and idle notices: a reply is a new SendMessage to the sender's `from` address;
  `notify_when_idle` asks the receiver for a `peer_idle_notice {state, finished_at, detail}` once
  it goes idle after handling the message.
- F8 Gating: `CLAUDE_CODE_HARBOR_KITE` overrides the GrowthBook gate `tengu_harbor_kite` either
  way, so `CLAUDE_CODE_HARBOR_KITE=0` turns SendMessage/ListAgents off. A settings
  `permissions.deny` rule also blocks a tool by name.
- F9 Docker: AF_UNIX connect works between containers only because they share one kernel.
- F10 ccw today: per-session `CLAUDE_CONFIG_DIR=/sessions/<id>/config`, child env from an
  allow-list (session.py `build_env`), session ids `ses_` plus 12 hex, uid 1000 `agent`. The ccw
  MCP server (mcp_server.py) exposes one `command` tool; router.py `_on_pre_tool_use` denies every
  `mcp__ccw__*` call that carries an `agent_id`. router.py `_on_stop` awaits `_after_turn`. API
  errors are `{"error": {"code", "message"}}`. Hooks: config.py `HOOK_EVENTS`, `VETO_EVENTS`;
  hooks.py `EVENT_DECISIONS` and `HookDispatcher` (built per session by manager.py `_dispatcher`,
  but constructible with any cwd). `sessions.exit_when_last_destroyed` requests exit from
  manager.py `_maybe_exit_when_empty` after the last destroy.

Why native sockets were dropped: F9 is kernel-local, so pods on different nodes cannot connect
even through a shared RWX file. Why the shared folder was dropped: user decision D5.

## Decisions

### D1: ccw-owned peer messaging over HTTP replaces native SendMessage/ListAgents

User decision: custom tooling. ccw carries messages between instances over its public HTTP API
and the agent uses ccw MCP tools. Rejected: native Unix-socket messaging (does not cross nodes), a
shared pid namespace, mirroring Claude's registry, a message broker. Serves A.

### D2: Opt-in `peers:` config section

`peers: {enabled: false, name: "", advertise_url: "", token: "", static: [], heartbeat_seconds:
10, max_missed_heartbeats: 3, join_timeout_seconds: 10, max_message_bytes: 65536, inbox_max: 32,
max_clock_skew_seconds: 60, max_messages_per_minute: 30}` (defaults my call). Validation at config
load, each error at its `peers.<key>` path:

- `token` is required when enabled ("required when peers.enabled; set it, normally from
  ${CCW_PEER_TOKEN}"), at least 16 characters, and must differ from `server.auth_token` (my call).
- `name`: empty means the host name at runtime; otherwise `[A-Za-z0-9._-]{1,64}`, not `.`, `..`
  or starting with `.`.
- `advertise_url`: empty means `http://<hostname>:<bound public port>`; otherwise an absolute
  `http` or `https` URL without a trailing slash; `{port}` is replaced by the bound public port
  (my call).
- `static`: a list of seed URLs, each a string or `{url}` (both accepted, normalized to strings,
  my call), absolute http(s) without a trailing slash, de-duplicated; `${VAR}` interpolation works
  as everywhere. `ccw serve --peer URL` (repeatable) appends to it at startup (my call: extends,
  never replaces). Our own advertise url in the list is ignored at runtime.
- `heartbeat_seconds > 0`, `max_missed_heartbeats >= 1`, `join_timeout_seconds > 0`,
  `1 <= max_message_bytes <= 1048576`, `inbox_max >= 1`, `max_clock_skew_seconds > 0`,
  `max_messages_per_minute >= 1`.
- With peers enabled a user `claude.mcp_servers.ccw` is a config error.

`Config.redacted()` masks `peers.token`; the service adds the token to the redactor. Instance
names must be unique within a mesh; that is an operator rule stated in the README. Serves B.

### D3: Native built-ins off when peers are enabled

Per session, only when enabled: `CLAUDE_CODE_HARBOR_KITE=0` as a managed env var; settings.json
`permissions.deny` gains `SendMessage` and `ListAgents` and `permissions.allow` gains
`mcp__ccw__peers` and `mcp__ccw__send_message`; the PreToolUse gate denies `SendMessage` and
`ListAgents` with the reason `use mcp__ccw__send_message / mcp__ccw__peers`. The subagent deny
reason is reworded to cover every `mcp__ccw__*` tool (my call). Disabled, none of this is written.
Serves C.

### D4: ccw MCP tools `peers` and `send_message`

Listed only when peers are enabled; with peers on the ccw MCP server is injected even with an
empty agent allowlist. Main agent only through the existing `mcp__ccw__*` subagent deny.

- `peers {instance?: string}`: JSON text, `[{address, instance, session_id, name, cwd}]` for the
  sessions of reachable members plus this instance's other sessions, excluding the caller, sorted
  by address, filtered by `instance`.
- `send_message {to, message, notify_when_idle?}`: validates `to` as `<instance>/<session id>`
  and not the caller, the message as non-empty and at most `max_message_bytes` UTF-8 bytes,
  resolves `to` to a reachable member's url (or this instance), generates `msg_` plus 16 hex,
  POSTs it, and returns `Sent <msg_id> to <to>. A reply, if any, arrives as a new turn.` or an
  error result whose text starts with `unknown peer`, `peer unreachable`, `message too large`,
  `inbox full`, `rate limited`, `peer rejected the request` or `invalid arguments`.

`handle_message` becomes async with an optional `peers` argument; the `command` tool is unchanged.
The CLAUDE.md that ccw writes gets a short peers paragraph when enabled (reply only when a reply
is needed, never use SendMessage). A send to a session of the same instance goes through the same
signed route on our own url (my call: one code path). Serves D.

### D5: Discovery is membership: join, leave, static seeds, heartbeats

User decision, verbatim: "No.. wrapper supports join/leave endpoints. Config/arguments allow to
provide static list of peers for discovery. Leave/join happens on container leave/join. Join
operation precedes start hook, leave precedes last hook in lifecycle. New hooks exist pre/postJoin
and pre/postLeave. Heartbeat keeps reachable agents list up to date."

Interpretation (all of it my call unless quoted above):

- Membership is per instance (container), keyed by instance name, each member `{instance, url,
  sessions: [{session_id, name, cwd}], last_seen, missed}`.
- Route (signed, D6; heartbeat only, D16): `POST /v1/peers/heartbeat` body `{instance, url,
  sessions, members, leaving}` answers 200 with the target's view `{instance, url, sessions,
  members: [{instance, url}]}`. A heartbeat from a member adds or refreshes it as reachable with the
  sessions it reported; a heartbeat with `leaving: true` removes it at once. A known name with a
  new url takes the new url (a restarted container). There is no join or leave route.
- Sessions are only taken from a member's own report (its heartbeat, or its answer to ours);
  `members` lists are gossip of `{instance, url}` used only to learn whom to contact. A member is
  reachable only after a direct successful exchange. One reachable seed is therefore enough and
  the mesh converges within a few heartbeats.
- Startup: after the ports are bound and before the manager accepts creates, ccw fires preJoin
  (VETO: a deny means this instance stays standalone, logged, counted and noted in the postJoin
  input), runs the first heartbeat round (the join) to every seed and known url concurrently, each
  bounded by `join_timeout_seconds`, then fires postJoin with `{instance, url, members,
  failures}`. Failed seeds stay in the contact list and are retried by every heartbeat. Startup
  never waits longer than the hook timeouts plus `join_timeout_seconds`.
- Heartbeat: every `heartbeat_seconds` the instance POSTs its view and sessions to every known
  url concurrently (timeout `heartbeat_seconds`), merges the answers, counts a miss per failure; a
  member with `max_missed_heartbeats` consecutive misses leaves the reachable list (its url stays a
  contact so it can come back). A session create or destroy triggers one immediate heartbeat round
  (debounced to at most one in flight) so peers learn it promptly. A standalone (preJoin-denied)
  instance does not heartbeat but still answers others.
- Shutdown: before any session's onDestroy (SIGTERM, container stop, `exit_when_last_destroyed`,
  `exit_when_dead`), ccw stops the heartbeat loop, fires preLeave (observe-only: its decision is
  ignored, annotations kept), POSTs a final `leaving: true` heartbeat (no sessions) to every known
  member concurrently bounded by `join_timeout_seconds`, and fires postLeave with `{instance, url,
  members, failures}`, then the sessions shut down.
  For `exit_when_last_destroyed` the leave runs before the last session's onDestroy when that
  destroy will empty the service (my call: the destroy path checks the policy first).
- The member table is bounded (1024 members, my call; a new member beyond it gets 429
  `rate_limited`).
- All membership I/O runs as background tasks or with bounded timeouts; the session lifecycle
  and the event loop never wait on a peer beyond the join and leave bounds above.

Serves E.

### D6: Peer HTTP API and request signing

Mounted on the public app only when enabled, outside the `/v1` bearer dependency, all under
`/v1/peers/`: `heartbeat` (D5, D16), `messages` (body `{from, to_session, message, msg_id,
notify_when_idle}`, 202 `{msg_id, queued}`) and `notices` (body `{from, to_session, msg_id,
status, state, reason}`, 202). Signing (D13) is the only auth; a bearer header never
authenticates. The body is capped before parsing at `max_message_bytes + 4096` bytes (413
`message_too_large`) and the signature is verified over the capped raw bytes before JSON parsing.
Errors: 401 `unauthorized`, 404 `not_found` (unknown or not-running session), 413
`message_too_large`, 422, 429 `inbox_full` or `rate_limited`. Serves F.

### D7: Inbox delivery as new turns

Each session has a FIFO inbox bounded by `inbox_max`; messages from one sender address to one
session are limited to `max_messages_per_minute` per sliding minute. Delivery takes the head only
when the session is ready with no turn, no command runner and no idle-runner command queued; it
submits with `origin: "peer"` (new origin, my call) and `metadata: {peer: {from, msg_id, kind}}`,
so onTurn may veto. `TurnRejected` keeps the head; a veto pops it with result `rejected`. After
every attempt the inbox re-checks the queue, and a kick that lands mid-attempt is honoured after
it. Close drops queued items with result `dropped`. Frame (body XML-escaped, my call):

```
<agent-message from="<instance>/<session>" msg_id="<msg_id>">
<message>
</agent-message>
Reply with mcp__ccw__send_message to "<instance>/<session>" only if a reply is needed.
```

Serves G.

### D8: Delivery notices

The target posts `/v1/peers/notices` to the sender instance's url for `rejected` and `dropped`
always, and for `delivered` when `notify_when_idle` asked for it (with the handling turn's terminal
state). Notices never produce notices. The POST is a background task, never awaited inside the
Stop hook path. A failure (after the one D14 retry) is logged and counted. Shutdown waits, bounded,
for pending notices. Frames: `<peer-idle-notice from=... msg_id=... state=.../>` with "The peer
finished the turn that handled your message <msg_id> and is idle." for delivered, and
`<peer-delivery-failed from=... msg_id=... status=... reason=.../>` with "Your message <msg_id>
was not delivered (<status>)." otherwise (my call). Serves H.

### D9: Events and metrics

EVENT_TYPES gain (before `session_closed`, my call) `peer_message_received`, `peer_message_sent`,
`peer_message_delivered` (keys as before: received `{msg_id, from, kind, bytes, message, queued}`,
sent `{msg_id, to, kind, bytes, notify_when_idle, result}`, delivered `{msg_id, from, kind,
turn_id, result}`), and `peer_member_joined` and `peer_member_left` (`{instance, url, reason}`,
reason `join`, `heartbeat`, `leave` or `missed_heartbeats`), emitted on every live session's log
when the reachable list changes (my call: instance-level changes are visible on each session's
stream). Counters: `ccw_peer_messages_received_total{kind,result}`,
`ccw_peer_messages_sent_total{kind,result}`, `ccw_peer_messages_delivered_total{kind,result}`,
`ccw_peer_membership_total{op,result}` (op `join`, `heartbeat`, `leave` for the outbound rounds,
all heartbeat calls, and `inbound_heartbeat`), and the gauge `ccw_peer_members`. Serves G and E.

### D10: Compose example

`examples/docker-compose.peers.yml` runs `ccw-a` and `ccw-b` from one build (`build: ..`), each
with its own state volumes and loopback port, one `CCW_PEER_TOKEN`, names `a` and `b`, advertise
urls `http://ccw-a:8080` and `http://ccw-b:8080`, and `peers.static` pointing at the other
service's url, from `examples/peers.config.yaml` (a committed symlink into the package data). No
shared volume. Serves J.

### D11: README

A `## Peer messaging` section: what it replaces and why; setup (enable, token, names, advertise
urls, `peers.static` and `--peer`); membership (heartbeat only: the first round joins, a
leaving heartbeat leaves; gossip, reachability, the startup and shutdown order and their bounds);
addressing `<instance>/<session id>`; unique names; request signing; delivery and frames; notices; delivery guarantees (at most once, a restart
loses the in-memory inbox, the dedupe window of 256 `(from, msg_id, kind)` per session, one client
retry); limits; cross-node and cross-cluster use (every instance's `advertise_url` reachable from
every other, clocks within `max_clock_skew_seconds`); TLS (ccw signs but does not encrypt and does
not terminate TLS, so cross-host traffic needs TLS in front); trust model (one shared token is one
trust domain, any holder can push prompts into bypass-mode agents, claim any `from` and join the
mesh, any agent running as the agent uid can read the token from the ccw process env, main-agent
only is not enforced against a subagent calling `/internal/mcp` from Bash). Plus a pointer from
`## Trust model`, the compose example, a Kubernetes note (per-pod advertise urls, a headless
Service for seeds), config rows, the three routes, the error codes, events, metrics, the four
hooks, the `peer` origin and the two tools. Serves I and L.

### D12: Tests and conventions

The fake claude substitutes `{{name}}` with the named groups of the turn's `match` regex in its
step strings. Scenarios anchor on the message body (`ping` replies `pong`, `pong` matches no
sending turn), so the exchange cannot loop. tests/conftest.py gains a `ccw_service` fixture
(run_service on port 0) and `World.service_cfg(name, ...)`. Unit tests per module;
tests/test_peers_e2e.py runs three services on real ports, B seeded with A and C seeded with B
only; tool results are read from `fake-mcp.jsonl` and `tool_result` events. A docker two-container
test (`scripts/integration-peers.sh`, `mise run test:integration:peers`, also run by
`mise run test:integration` with the image built once) proves join, listing, messages both ways
and leave on stop. Real-claude acceptance of the tool schemas is cannotVerify. Serves K and M.

### D13: HMAC request signing

The peer token is a signing key only. Headers `X-CCW-Peer-Timestamp` (unix ms), `X-CCW-Peer-Nonce`
(32 hex), `X-CCW-Peer-Signature` (hex HMAC-SHA256 over
`<METHOD>\n<path>\n<timestamp>\n<nonce>\n<sha256 hex of body>`). The receiver rejects a timestamp
outside `max_clock_skew_seconds`, a nonce already seen (cache kept for twice the window, capped at
65536; when full, 429 `rate_limited` rather than evicting a live nonce, my call), and a bad
signature, with `hmac.compare_digest`. Every peer route, including heartbeat, is signed. Serves N.

### D14: Delivery guarantees and retries

Delivery is at most once; a restart loses queued items. The client retries once, with the same
`msg_id` and a fresh nonce and timestamp, on connection-level failures only; the receiver's dedupe
of the last 256 `(from, msg_id, kind)` per session makes that safe. Heartbeats are not retried
within a call (the next heartbeat is the retry). Serves N.

### D15: Instance-level membership hooks

`HOOK_EVENTS` gains `preJoin`, `postJoin`, `preLeave`, `postLeave`; `VETO_EVENTS` gains `preJoin`
(deny = standalone); the others honour only allow (preLeave annotations are kept, its decision
ignored). They fire on an instance-level `HookDispatcher` (cwd = `paths.sessions_root`, state dir
`<sessions_root>/.peers-hook-state`, my call) with the usual payload and `session_id` empty, and
`data` = `{instance, url, members: [{instance, url}]}` (post hooks add `failures: [{url, error}]`
and, for postJoin, `standalone: bool`). Existing `blocking`, `timeout_seconds` and `on_error`
semantics apply; results are logged and counted in `ccw_hook_actions_total`. They never fire with
peers disabled. Serves E.

### D16: Heartbeat only, no join or leave endpoint

User decision, verbatim: "Leave/join might be redundant with the static list, no? If yes keep
heartbeat only and drop the other 2 endpoints". They are redundant: the static seed list already
says whom to contact, a heartbeat already makes the sender reachable with its sessions, and missed
heartbeats already drop a silent member. So `POST /v1/peers/join` and `POST /v1/peers/leave` do
not exist; `POST /v1/peers/heartbeat` is the only membership route. The join is the first
heartbeat round (same preJoin, postJoin, ordering and `join_timeout_seconds` bound); the leave is
one final heartbeat with `leaving: true` and no sessions (same preLeave, postLeave, ordering and
bound), which the receiver turns into an immediate removal with reason `leave` (my call: a stopped
container leaves the listing at once instead of after `max_missed_heartbeats` intervals). The wire
view gains `leaving: bool = false`; `LeaveBody`, `accept_join`, `accept_leave`, `inbound_join` and
`inbound_leave` are gone. The four hooks, approval E's ordering and the outbound `join`, `heartbeat`
and `leave` metric ops stay. Serves O.

## Research

| Lens | Ran or skipped | Adopted | Rejected |
|---|---|---|---|
| Claude Code bundle reverse engineering | ran (scratch brief) | `CLAUDE_CODE_HARBOR_KITE=0` and settings deny (F8); idle-notice shape (F7) | native socket transport and registry (F1, F5, F9) |
| ccw codebase | ran (mcp_server.py, internal_api.py, router.py, manager.py, service.py, api/, bootstrap/, hooks.py, config.py, events.py, metrics.py, fake claude, conftest) | ccw MCP server, `mcp__ccw__*` subagent deny, `submit_turn` with a new origin, `on_state` ready trigger, `build_env` managed vars, `render_settings`, `HookDispatcher` at instance level, `VETO_EVENTS` | typing into a busy PTY; awaiting network I/O in `_on_stop` |
| Plan review panel and builder check | ran (coordinator verdict) | HMAC signing (D13), notices off the Stop path (D8), at-most-once and retry (D14), inbox re-check (D7), rate limit (D7), README serialization | Bearer token on the wire |
| User review of discovery | ran (user verbatim, D5) | static seeds, pre/post join and leave hooks, heartbeats; then heartbeat only (D16) | shared-folder discovery; join and leave endpoints (D16) |
| External prior art | skipped: gossip-style membership is standard; the open questions are ccw-internal | - | - |

## Research inputs

| Source | Entry | Lands in |
|---|---|---|
| brief section 5 (F8) | `CLAUDE_CODE_HARBOR_KITE` overrides the gate; deny rules block by name | peer-session-bootstrap |
| brief section 4 (F7) | reply to `from`; idle notice after handling | peer-inbox-outcomes, peer-service-core |
| brief section 7 (F9) | AF_UNIX is kernel-local | rejected: native transport dropped (D1) |
| brief section 1 (F1, F5) | native registry collides across containers | rejected: membership keyed by instance name (D5) |
| brief section 8 fix 5 | ccw-side relay over HTTP through an MCP tool | peers-mcp-tools, peer-messages-endpoint, peer-http-client |
| user review, verbatim | static seeds, ordering, four hooks, heartbeat; join and leave endpoints dropped as redundant (D16) | peer-member-table, peer-membership-service, peer-membership-hooks, peers-join-leave-order, peer-messages-endpoint, peer-http-client |
| router.py `_on_stop` awaits `_after_turn` | network I/O there delays the Stop answer | peer-inbox-outcomes |
| hooks.py `HookDispatcher`, config.py `VETO_EVENTS` | reusable at instance level | peer-membership-hooks |
| plan review items 1, 7 to 9 | signing, inbox re-check, rate limit, retry | peer-auth-signing, peer-inbox-delivery, peer-http-client |

## Decision coverage

| Decision | Tasks |
|---|---|
| D1 ccw-owned HTTP messaging | peer-http-client, peer-messages-endpoint, peer-service-core, manager-peers-lifecycle |
| D2 Opt-in peers config | peers-config-section, manager-peers-lifecycle, peers-join-leave-order |
| D3 Native built-ins off | peer-session-bootstrap, manager-peers-lifecycle |
| D4 MCP tools | peers-mcp-tools, peer-session-bootstrap, peer-service-core |
| D5 Membership discovery | peer-member-table, peer-membership-service, peer-messages-endpoint, manager-peers-lifecycle, peers-join-leave-order |
| D6 Peer HTTP API | peer-messages-endpoint, peer-http-client |
| D7 Inbox delivery | peer-inbox-delivery, peer-service-core |
| D8 Delivery notices | peer-inbox-outcomes, peer-service-core, peers-two-instance-e2e |
| D9 Events and metrics | peer-events-and-metrics, peer-inbox-delivery, peer-membership-service, peer-service-core |
| D10 Compose example | peers-compose-example |
| D11 README | peers-config-section, peer-events-and-metrics, peer-membership-hooks, peer-messages-endpoint, peers-readme-guide |
| D12 Tests and conventions | fake-claude-peer-harness, peers-two-instance-e2e, peers-two-container-test |
| D13 HMAC signing | peer-auth-signing, peer-http-client, peer-messages-endpoint |
| D14 Delivery guarantees and retries | peer-http-client, peer-inbox-delivery, peers-readme-guide |
| D15 Membership hooks | peer-membership-hooks, peer-membership-service, peers-join-leave-order |
| D16 Heartbeat only | peer-http-client, peer-messages-endpoint, peer-member-table, peer-membership-service, peer-service-core, peer-events-and-metrics, peer-membership-hooks, peers-join-leave-order, peers-two-instance-e2e, peers-two-container-test, peers-readme-guide |

## Contracts

| Part | Owner | Consumers | Where defined |
|---|---|---|---|
| `fake-match-substitution` | fake-claude-peer-harness | peers-two-instance-e2e, peers-two-container-test | `tests/fake_claude/fake_claude.py` |
| `ccw-service-fixture` | fake-claude-peer-harness | peers-two-instance-e2e | `tests/conftest.py` |
| `peers-config` | peers-config-section | peer-membership-hooks, peer-inbox-delivery, peer-messages-endpoint, peer-session-bootstrap, peer-membership-service, peer-service-core, manager-peers-lifecycle, peers-compose-example | `src/claude_code_wrapper/config.py` |
| `peer-auth` | peer-auth-signing | peer-http-client, peer-messages-endpoint | `src/claude_code_wrapper/peer_auth.py` |
| `peer-member-table` | peer-member-table | peer-http-client, peer-membership-service, peer-service-core | `src/claude_code_wrapper/peers.py` |
| `peer-events` | peer-events-and-metrics | peer-inbox-delivery, peer-service-core | `src/claude_code_wrapper/events.py` |
| `peer-metrics` | peer-events-and-metrics | peer-inbox-delivery, peer-messages-endpoint, peer-membership-service, peer-service-core | `src/claude_code_wrapper/metrics.py` |
| `peer-membership-hooks` | peer-membership-hooks | peer-membership-service, manager-peers-lifecycle | `src/claude_code_wrapper/hooks.py` |
| `peer-wire` | peer-http-client | peer-inbox-delivery, peer-messages-endpoint, peers-mcp-tools, peer-membership-service, peer-service-core | `src/claude_code_wrapper/peer_client.py` |
| `peer-inbox` | peer-inbox-delivery | peer-inbox-outcomes, peer-service-core, manager-peers-lifecycle | `src/claude_code_wrapper/peer_inbox.py` |
| `peer-inbox-outcome` | peer-inbox-outcomes | peer-service-core | `src/claude_code_wrapper/peer_inbox.py` |
| `peer-messages-endpoint` | peer-messages-endpoint | peer-membership-service, peer-service-core, manager-peers-lifecycle | `src/claude_code_wrapper/api/peers.py` |
| `ccw-mcp-peers-tool` | peers-mcp-tools | peer-service-core, manager-peers-lifecycle | `src/claude_code_wrapper/mcp_server.py` |
| `peer-session-bootstrap` | peer-session-bootstrap | manager-peers-lifecycle | `src/claude_code_wrapper/bootstrap/settings.py` |
| `peer-membership` | peer-membership-service | peer-service-core | `src/claude_code_wrapper/peer_membership.py` |
| `peer-service` | peer-service-core | manager-peers-lifecycle | `src/claude_code_wrapper/peer_service.py` |
| `peers-compose` | peers-compose-example | peers-two-container-test | `examples/docker-compose.peers.yml` |

Cross-epic (docs/plans/contracts.json): this epic owns `peers-config`, `peer-member-table`,
`peer-messages-endpoint` and `ccw-mcp-peers-tool`, and consumes `ccw-mcp-tool` (owner
agent-callable-slash-command), `event-types` and `hook-events` and `hook-result-protocol` (owner
hook-capabilities-for-tatara: five event types, four hook events, preJoin as a veto event). The
earlier `peer-directory-entry` contract is retired (shared-folder discovery dropped).

## Review focus

- Zero change when off, including no hook fires and no outbound traffic.
- Ordering: no session create accepted before postJoin returns; leave and postLeave before any
  onDestroy on every shutdown path, including exit_when_last_destroyed; both bounded.
- Membership convergence and bounds: sessions only from a member's own report, reachability only
  from direct exchanges, miss counting, re-add, table cap, debounced immediate heartbeats.
- Signing on all three routes, verification over raw bytes before parse, nonce cache bounds, the
  token absent from every request.
- Delivery concurrency (peer-inbox-delivery) and notices off the Stop path (peer-inbox-outcomes).
- `handle_message` going async must not change the `command` tool.

## Left to the builder

- Internal structure of peer_auth.py, peers.py, peer_client.py, peer_inbox.py, peer_membership.py
  and peer_service.py within the stated interfaces.
- Wording of the peers paragraph, tool descriptions and README prose within the listed content.
- Poll intervals in tests within bounded deadlines.

## Assumptions and unknowns

- Real claude honours `CLAUDE_CODE_HARBOR_KITE=0` and the deny rules, and accepts the tool
  schemas; cannotVerify without credentials.
- Instances in one mesh have clocks within `max_clock_skew_seconds`; documented as an operator
  requirement.
- Every instance's `advertise_url` is reachable from every other; documented as an operator
  requirement (members that are not reachable simply never become reachable).

## Open questions

none

## Irreversible operations in scope

none
