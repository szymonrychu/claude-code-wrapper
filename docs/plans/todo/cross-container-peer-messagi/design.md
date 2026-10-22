# Cross-container peer messaging over a shared mount design

## Summary

ccw gets its own peer messaging for the main agents of its sessions, working across containers,
nodes and clusters. An opt-in `peers:` section (off by default, zero change when off) turns it on.
Each ccw instance publishes one signed, heartbeat-refreshed JSON file per live session in a shared
directory (`/peers` by default, may be an RWX volume such as CephFS). The ccw MCP server gains
`peers` and `send_message`. A send is an HMAC-signed HTTP POST to the target instance's
`POST /v1/peers/messages`; the shared peer token is the signing key and never travels on the wire.
The target queues the message in the session's inbox and delivers it as a new turn when the
session is idle, framed as an `<agent-message>`. The target posts a notice back for a rejected or
dropped message always, and for a delivered one when `notify_when_idle` asked for it. Claude's
native SendMessage/ListAgents are switched off for peered sessions.

## Intent and why

The user runs several ccw containers, possibly on different Kubernetes nodes, whose main agents
must talk to each other. The first plan used Claude Code's native peer messaging over Unix sockets
on a shared mount. The user rejected it: a Unix socket is a kernel object and never crosses a node,
even when the socket file sits on an RWX CephFS volume (F9 shows it works only inside one kernel).
User decision: "go with custom tooling", ccw-owned messaging over its HTTP API that must work
across containers, nodes and clusters.

## Goals

- `peers.enabled: false` (the default): argv, env, settings.json, .claude.json, CLAUDE.md, the
  mounted routes, the MCP tool list and the filesystem are exactly as today.
- `peers.enabled: true`: every session is published in `<directory>/<instance>/<session id>.json`
  while it lives, signed, refreshed every `heartbeat_seconds`, and removed on every stop path.
- `mcp__ccw__peers` lists live, correctly signed peers other than the caller;
  `mcp__ccw__send_message` delivers to a peer on any instance that can reach the target's
  `advertise_url`, or returns a clear error.
- A delivered message is a new turn on the target session, in arrival order, never typed into a
  busy PTY, and no queued item is ever stranded; a reply and the notices reach the sender the same
  way.
- A sender always learns when its message was rejected or dropped.
- SendMessage and ListAgents are unavailable to peered sessions (env, settings deny, tool gate).
- The peer token is never sent on the wire, never logged and never written to a file; peer
  requests are signed, time-bounded and replay-protected.
- A stalled shared filesystem degrades to errors and empty listings, never freezes sessions.
- Events `peer_message_received`, `peer_message_sent`, `peer_message_delivered` and Prometheus
  counters exist and are documented.
- Two ccw services in one process on real HTTP ports, and two containers under compose, complete a
  send, a reply and an idle notice with the fake claude.
- README documents setup, addressing, every key, every route, the trust model, delivery guarantees,
  cross-node use and the TLS note; the image and a compose example work with a fresh named volume.

## Non-goals

- Native Claude peer messaging: `--messaging-socket-path`, `crossSessionInbound`, a shared pid
  namespace, Claude's session registry.
- A static peer list, a ccw-to-ccw listing API or socket probes; discovery is the shared directory.
- TLS termination or payload encryption inside ccw (ccw signs, it does not encrypt).
- Messaging from subagents or teammates.
- At-least-once delivery or a persistent inbox; delivery is at most once.
- Testing the tool schemas against a real logged-in claude.

## Anti-goals

- Any behaviour change for configs without `peers.enabled: true`.
- A message typed into a running turn, two peer turns interleaved, or a queued item left waiting
  while the session is idle.
- The peer token on the wire, in a log line, an event, `GET /v1/config`, the directory or a file in
  a session dir.
- A peer request accepted with a bad signature, a stale timestamp or a replayed nonce; a
  `server.auth_token` bearer opening a peer route, or the peer token opening any other route.
- A Stop hook answer held up by a notice POST to a slow or dead sender.
- Directory I/O on the event loop.
- Two agents replying to each other forever without a bound.
- An unbounded inbox, nonce cache, directory or request body.

## Constraints

- Repo CLAUDE.md: test-first (red, green, refactor), always `uv run`, plain ASCII, no em dashes,
  never write service files into a session workdir.
- Tests use the fake claude under tests/fake_claude only; only the fake-claude task
  (fake-claude-peer-harness) edits tests/conftest.py.
- Every config key needs a README row (tests/test_readme_config.py); every route needs a README
  heading (tests/test_readme_api.py); every event type needs a README heading under
  `## Events (SSE)`; every API error code is documented under `## Errors`.
- README.md is edited by exactly four tasks, serialized through dependsOn: peers-config-section,
  peer-events-and-metrics, peer-messages-endpoint, peers-readme-guide.
- examples/*.config.yaml are committed symlinks into src/claude_code_wrapper/data/examples.
- The EVENT_TYPES pin in tests/test_event_log.py is updated, not loosened.
- Tasks of about an hour, at most 8 files.

## Facts (background: native messaging research, carried from the scratch brief)

Source: reverse engineering of the claude 2.1.307 (macOS) and 2.1.304 (Linux, from the image)
bundles plus throwaway Docker experiments with `--network none`, no credentials. These facts are
why the native design was planned and why it was dropped; the new design keeps only F8 (how to
switch the built-ins off), F10 (ccw today) and the shape of F7 (reply and idle-notice semantics).

- F1 Registry: native discovery is `$CLAUDE_CONFIG_DIR/sessions/<pid>.json`, keyed by pid with no
  uniqueness check; a foreign pid domain is listed only if its socket accepts a 250 ms connect.
- F2 Socket path: `--messaging-socket-path` must be absolute, at most 103 bytes, in a 0700 dir
  owned by the uid; any vetting failure silently falls back to `/tmp/cc-socks-<uid>`.
- F3 Wire: NDJSON over AF_UNIX; the auth line is optional off Windows.
- F5 Pid collision: two containers sharing one registry both ran claude as pid 10 and one record
  overwrote the other.
- F6 Inbound policy: a bypass-mode receiver holds unasserted peer messages for a TUI approval
  unless `crossSessionInbound: accept`.
- F7 Replies and idle notices: a reply is a new SendMessage to the sender's `from` address;
  `notify_when_idle` asks the receiver for a `peer_idle_notice {state, finished_at, detail}` once
  it goes idle after handling the message.
- F8 Gating: the tools sit behind the GrowthBook gate `tengu_harbor_kite` (default true);
  `CLAUDE_CODE_HARBOR_KITE` overrides it either way, so `CLAUDE_CODE_HARBOR_KITE=0` turns
  SendMessage/ListAgents off. A settings `permissions.deny` rule also blocks a tool by name.
- F9 Docker: AF_UNIX connect works between containers through a named volume or bind mount only
  because both containers share one kernel. A fresh named volume is root:root 755 unless the image
  pre-creates the mount point owned by the user, which Docker copies into the new volume.
- F10 ccw today: per-session `CLAUDE_CONFIG_DIR=/sessions/<id>/config`, child env from an
  allow-list (session.py `build_env`), session ids `ses_` plus 12 hex (manager.py
  `_new_session_id`), uid 1000 `agent`. The ccw MCP server (mcp_server.py) exposes one `command`
  tool on the loopback internal API; router.py `_on_pre_tool_use` already denies every
  `mcp__ccw__*` call that carries an `agent_id` (main agent only). router.py `_on_stop` awaits
  `_after_turn`, so anything awaited there delays the Stop relay answer. API errors are
  `{"error": {"code", "message"}}` (api/errors.py `envelope`).

Why native sockets were dropped: F9 is kernel-local by nature. On Kubernetes, pods on different
nodes share an RWX CephFS file but not a kernel, so a connect to the socket file fails. Making it
work would need a same-node scheduling constraint, which defeats the use case. F1, F2, F5 and F6
add four more fragile couplings to undocumented Claude internals. HTTP between ccw instances has
none of these limits and stays under ccw's control.

## Decisions

### D1: ccw-owned peer messaging over HTTP replaces native SendMessage/ListAgents

User decision: custom tooling. ccw carries messages between instances over its public HTTP API
and the agent uses ccw MCP tools. Rejected: native Unix-socket messaging on a shared mount (does
not cross nodes, see "Why native sockets were dropped"), a shared pid namespace, mirroring
Claude's registry, a message broker (new infrastructure the user did not ask for). Serves A.

### D2: Opt-in `peers:` config section

`peers: {enabled: false, name: "", advertise_url: "", token: "", directory: "/peers",
heartbeat_seconds: 10, stale_seconds: 30, max_message_bytes: 65536, inbox_max: 32,
max_clock_skew_seconds: 60, max_messages_per_minute: 30}` (defaults my call; the last two from
the plan review). Validation at config load, each error at its `peers.<key>` path:

- `token` is required when enabled ("required when peers.enabled; set it, normally from
  ${CCW_PEER_TOKEN}"), at least 16 characters (my call), and must differ from
  `server.auth_token` (my call).
- `name`: empty means the host name at runtime; otherwise `[A-Za-z0-9._-]{1,64}` and not `.`,
  `..` or starting with `.` (it is a directory name on the shared volume).
- `advertise_url`: empty means `http://<hostname>:<bound public port>`; otherwise an absolute
  `http` or `https` URL without a trailing slash; the literal `{port}` is replaced by the bound
  public port (my call: lets tests and port-0 setups advertise a port they do not know yet).
- `directory`: absolute path.
- `heartbeat_seconds > 0`, `stale_seconds > heartbeat_seconds`, `1 <= max_message_bytes <=
  1048576`, `inbox_max >= 1`, `max_clock_skew_seconds > 0`, `max_messages_per_minute >= 1`.
- With peers enabled a user `claude.mcp_servers.ccw` is a config error (the name is reserved).

`Config.redacted()` masks `peers.token`; the service adds the token to the redactor. Names must be
unique across instances sharing a directory; that is an operator rule stated in the README, not
enforced at startup (D5). Serves B and N.

### D3: Native built-ins off when peers are enabled

Per session, only when enabled: the child env gets `CLAUDE_CODE_HARBOR_KITE=0` as a managed var
(user env cannot override it, my call); the ccw-written settings.json gets `SendMessage` and
`ListAgents` in `permissions.deny` and `mcp__ccw__peers` and `mcp__ccw__send_message` in
`permissions.allow`; the PreToolUse gate in router.py denies `SendMessage` and `ListAgents` with
the reason `use mcp__ccw__send_message / mcp__ccw__peers`, emitting `tool_denied` and counting a
deny. The subagent deny reason is reworded to cover every `mcp__ccw__*` tool (my call). Disabled,
none of this is written. Serves C.

### D4: ccw MCP tools `peers` and `send_message`

Registered in `tools/list` only when peers are enabled; with peers on the ccw MCP server is
injected into `.claude.json` even when the agent command allowlist is empty. Main agent only
through the existing `mcp__ccw__*` subagent deny.

- `peers {instance?: string}`: JSON text, a list of `{address, instance, session_id, name, cwd}`
  for live, correctly signed entries, excluding the caller, sorted by address (my call), filtered
  by `instance` when given.
- `send_message {to: string, message: string, notify_when_idle?: bool}`: validates `to` as
  `<instance>/<session id>` and not the caller (my call), the message as non-empty and at most
  `max_message_bytes` UTF-8 bytes, looks `to` up among live entries, generates
  `msg_id = "msg_" + 16 hex`, POSTs to the target, and returns
  `Sent <msg_id> to <to>. A reply, if any, arrives as a new turn.` or an error result
  (`isError: true`) whose text starts with one of: `unknown peer`, `peer unreachable`,
  `message too large`, `inbox full`, `rate limited`, `peer rejected the request`,
  `invalid arguments`.

`handle_message` becomes async with an optional `peers` argument (`PeerTools` protocol); the
`command` tool behaves exactly as before. `initialize` instructions and the CLAUDE.md that ccw
writes get a short peers paragraph when enabled: what the tools do, the address format, reply to
an `<agent-message>` with `mcp__ccw__send_message` to its `from` only when a reply is needed, never
use SendMessage. Serves D.

### D5: Discovery is a directory of signed, heartbeat-refreshed JSON files

`<directory>/<instance>/<session id>.json` holds `{instance, session_id, name, cwd, url,
started_at, heartbeat_at, sig}` (`name` is the session name or the session id, `url` the
instance's advertise url, times ISO 8601 UTC with millisecond precision, `sig` the D13 entry
signature). Written atomically (temp file in the same dir, then rename) after the session's
onCreate/onResume gate passes, rewritten every `heartbeat_seconds` by one instance-wide heartbeat
task (a missing file is recreated), removed in the manager teardown that every stop path runs. An
entry is live when `now - heartbeat_at <= stale_seconds` and its signature verifies; unsigned,
badly signed, unreadable or malformed files are ignored. There are no socket probes.

At startup ccw creates `<directory>/<instance>/` if missing and deletes every entry in it, logging
a warning when any of them looked live (names must be unique; a live-looking entry means a recent
crash or a misconfigured twin). Every heartbeat pass also deletes, under any instance name, entries
whose `heartbeat_at` is older than `10 x stale_seconds` (per-session files, so this never races a
live writer) and then removes empty instance dirs, so dead instances do not accumulate and
`live()` stays bounded. No static peer list, no listing API.

All directory file I/O runs in `asyncio.to_thread` under a timeout (5 s, my call, constant
`DIRECTORY_IO_TIMEOUT`); a timeout or OSError makes `live()` return what it read so far (or
nothing), `publish` and `remove` raise or log a `PeersError`, and the heartbeat loop logs and goes
on. Session turns never wait on the directory. Serves E and N.

### D6: Peer HTTP API and auth

Mounted on the public app only when enabled, outside the `/v1` bearer dependency:

- `POST /v1/peers/messages` body `{from, to_session, message, msg_id, notify_when_idle}` ->
  202 `{msg_id, queued}` (`duplicate: true` for a repeat).
- `POST /v1/peers/notices` body `{from, to_session, msg_id, status, state, reason}` -> 202
  `{msg_id, queued}`; `from` is the session that received the original message, `to_session` the
  original sender, `status` one of `delivered`, `rejected`, `dropped`, `state` the handling turn's
  terminal state for `delivered` (else null), `reason` a short text for the others.

Auth is D13 request signing: missing, malformed or bad signature, a timestamp outside
`max_clock_skew_seconds`, or a nonce seen within the window is 401 `unauthorized`. A bearer
header, including `server.auth_token` or the peer token itself, is ignored and does not
authenticate. The peer token is never accepted on other routes (they compare a bearer against
`server.auth_token` only, and D2 keeps the tokens distinct). The body is capped before parsing at
`max_message_bytes + 4096` bytes (my call: room for the envelope), checking `Content-Length` first
and counting streamed bytes after, so an oversize body is 413 `message_too_large` without being
buffered whole; the signature is verified over the capped raw bytes before JSON parsing. A message
over `max_message_bytes` is also 413 `message_too_large`; an unknown or not-running session is 404
`not_found`; a full inbox is 429 `inbox_full`; a sender over `max_messages_per_minute` toward that
session is 429 `rate_limited`; a malformed body is 422 like other routes. Serves F.

### D7: Inbox delivery as new turns

Each session has a FIFO inbox bounded by `inbox_max` (messages and notices together). Messages
(not notices) from one sender address to one session are limited to `max_messages_per_minute` in a
sliding 60 s window (rate limit from the plan review, against reply loops). Delivery takes the head
only when the session is `ready`, has no current turn, no command runner is active and no command
is queued for the idle runner; it submits through `SessionRouter.submit_turn` with
`origin: "peer"` (a new turn origin, my call) and `metadata: {peer: {from, msg_id, kind}}`, so the
onTurn hook sees and may veto it. A `TurnRejected` (busy, booting, dead) leaves the head in place
for the next `ready`; a veto pops it with `peer_message_delivered` result `rejected`. After every
attempt, whatever its outcome, the inbox re-checks the queue; a `kick()` that arrives while an
attempt runs is recorded and honoured when the attempt ends, so no item is stranded while the
session is idle. Triggers: an accepted item, every `ready` state change and the end of an attempt,
each scheduled on the next loop pass. On session close the queued items are dropped with result
`dropped`.

Message framing (body XML-escaped for `&`, `<`, `>` so it cannot close the frame, my call):

```
<agent-message from="<instance>/<session>" msg_id="<msg_id>">
<message>
</agent-message>
Reply with mcp__ccw__send_message to "<instance>/<session>" only if a reply is needed.
```

Serves G.

### D8: Delivery notices

The target posts `POST /v1/peers/notices` to the sender instance's `url` (from the live entry of
the sender session) for: `rejected` (onTurn veto) and `dropped` (session close or shutdown with the
message still queued), always; `delivered` when the message asked for `notify_when_idle`, once its
handling turn reaches a terminal state, with that state. Notices themselves never produce notices.
The POST runs as a background task (`SessionRouter._spawn` or the peer service's own task set),
never awaited inside the Stop hook path, so a slow or dead sender cannot delay the Stop relay
answer. A missing entry or a failed POST (after the one D14 retry) is logged and counted, never
re-queued. On shutdown the manager waits, bounded by the shutdown budget, for pending notice tasks
before closing the client. The sender ccw queues a notice under D7 rules (no rate limit), framed as

```
<peer-idle-notice from="<instance>/<session>" msg_id="<msg_id>" state="<state>"/>
The peer finished the turn that handled your message <msg_id> and is idle.
```

for `delivered`, and for `rejected` or `dropped`

```
<peer-delivery-failed from="<instance>/<session>" msg_id="<msg_id>" status="<status>" reason="<reason>"/>
Your message <msg_id> was not delivered (<status>).
```

(second frame, my call). Serves H and N.

### D9: Events and metrics

New EVENT_TYPES (appended before `session_closed`, my call; the pin in tests/test_event_log.py is
updated):

- `peer_message_received` on the target session: `{msg_id, from, kind, bytes, message, queued}`.
- `peer_message_sent` on the sending session: `{msg_id, to, kind, bytes, notify_when_idle,
  result}` with result `ok` or the error code; a notice is logged on the session that received
  the original message, with its `status`.
- `peer_message_delivered` on the target session: `{msg_id, from, kind, turn_id, result}` with
  result `delivered`, `rejected` or `dropped`.

Counters (my call): `ccw_peer_messages_received_total{kind,result}` (result `accepted`,
`duplicate`, `inbox_full`, `rate_limited`, `too_large`, `not_found`, `unauthorized`),
`ccw_peer_messages_sent_total{kind,result}`, `ccw_peer_messages_delivered_total{kind,result}`.
`kind` is `message` or `notice`. Serves G.

### D10: Image and compose example

The Dockerfile pre-creates `/peers` owned by `agent:agent` (uid 1000) so a fresh named volume is
writable (F9). `examples/docker-compose.peers.yml` runs `ccw-a` and `ccw-b` from one image build
(`build: ..`), each with its own workdir/sessions/mise volumes and loopback port, sharing one named
`peers` volume at `/peers` and one `CCW_PEER_TOKEN`, with instance names `a` and `b` and advertise
urls `http://ccw-a:8080` and `http://ccw-b:8080`, from `examples/peers.config.yaml` (a committed
symlink into the package data). Compose resolves every `-f` file's relative paths against the
first file's directory, so the CI override writes host paths relative to examples/ (`../tests/...`).
Serves J.

### D11: README

A `## Peer messaging` section: what it replaces and why, setup, addressing
(`<instance>/<session id>`), unique instance names, the directory layout, signing and liveness,
delivery and framing, notices, delivery guarantees (at most once; a restart loses the in-memory
inbox; the dedupe window; one client retry), limits (`max_message_bytes`, `inbox_max`,
`max_messages_per_minute`, body cap, clock skew), cross-node and cross-cluster use through an RWX
volume (clocks NTP-synced within `max_clock_skew_seconds` and `stale_seconds`), the TLS note (ccw
signs but does not encrypt and does not terminate TLS; cross-host traffic needs TLS in front, and
`advertise_url` may be https behind an ingress or mesh), and the trust model: one shared peer
token is one trust domain, any holder can push prompts into bypass-mode agents and claim any
`from`, any agent running as the agent uid can read the token from the ccw process env, and
main-agent-only is not enforced against a subagent calling `/internal/mcp` from Bash (same as the
command tool). Plus: a pointer from `## Trust model`, the compose example under
`## Docker and compose`, a note under `## Running in Kubernetes`, config rows for every key, the
two routes, the new error codes, the new events, the new metrics, the `peer` turn origin and the
two tools under `### \`POST /internal/mcp\``. Serves I and L.

### D12: Tests and conventions

The fake claude substitutes `{{name}}` with the named groups of the turn's `match` regex in the
string values of its steps (mcp arguments and text). Scenarios anchor on the message body
(`\nping\n</agent-message>` replies `pong`; a `pong` body matches nothing that sends), so the
ping-pong cannot loop. tests/conftest.py gains a `ccw_service` fixture that runs `run_service` on
real ports (port 0) and stops it at teardown, and `World.service_cfg(name, ...)` giving each
service its own workdir and sessions roots. Unit tests per module; tests/test_peers_e2e.py runs
two services on one tmp directory: A sends via `send_message`, B gets the framed turn, B replies,
A gets the reply turn and the idle notice; tool results are read from each session's
`fake-mcp.jsonl` and `tool_result` events. A docker integration test (`scripts/integration-peers.sh`,
mise task `test:integration:peers`, also run by `mise run test:integration` and so by CI, with the
image built once) proves the same between two containers. Real-claude acceptance of the tool
schemas is cannotVerify, not a task. All work is test-first, `uv run`, ASCII. Serves K and M.

### D13: HMAC request and entry signing

The peer token is a signing key only (plan review). New module peer_auth.py:

- Requests: headers `X-CCW-Peer-Timestamp` (unix milliseconds), `X-CCW-Peer-Nonce` (32 hex) and
  `X-CCW-Peer-Signature` (hex HMAC-SHA256 keyed by the token over
  `<METHOD>\n<path>\n<timestamp>\n<nonce>\n<sha256 hex of the body>`). The receiver rejects a
  timestamp more than `max_clock_skew_seconds` from its clock and a nonce already seen; the nonce
  cache keeps entries for twice the skew window and is capped (65536 entries, my call); when full
  after expiry, new requests are 429 `rate_limited` rather than evicting live nonces (my call).
  Comparison is `hmac.compare_digest`.
- Entries: `sig` = hex HMAC-SHA256 keyed by the token over the canonical JSON (sorted keys, no
  whitespace) of the seven other fields. Readers drop entries whose `sig` is missing or wrong.

Serves N.

### D14: Delivery guarantees and retries

Delivery is at most once: the inbox lives in memory, so a ccw restart loses queued items (and
their notices). The client retries once, with the same `msg_id` and a fresh nonce and timestamp,
on connection-level failures only (connect error, connect or read timeout, protocol error), never
on an HTTP answer; the receiver's dedupe of the last 256 `(from, msg_id, kind)` per session makes
that safe. Serves N.

## Research

| Lens | Ran or skipped | Adopted | Rejected |
|---|---|---|---|
| Claude Code bundle reverse engineering | ran (2.1.304 Linux, 2.1.307 macOS; scratch brief) | `CLAUDE_CODE_HARBOR_KITE=0` and settings deny to switch the built-ins off (F8); idle-notice shape with `state` (F7) | native socket transport (F2, F3, F6), native registry (F1, F5) |
| Docker experiments | ran (named volume and bind mount, two containers) | image-created mount point so a fresh named volume is writable (F9) | socket on a shared mount across nodes (F9 is kernel-local) |
| ccw codebase | ran (mcp_server.py, internal_api.py, router.py, manager.py, service.py, api/, bootstrap/, events.py, metrics.py, fake claude, conftest) | reuse the ccw MCP server, the `mcp__ccw__*` subagent deny, `submit_turn` with a new origin, the `on_state` ready trigger used by commands, `build_env` managed vars, `render_settings` permissions, `_spawn` for background work | typing into a busy PTY; awaiting network I/O in `_on_stop`; a new process or broker |
| Plan review panel and builder check | ran (coordinator verdict) | HMAC signing (D13), status notices off the Stop path (D8), at-most-once and retry (D14), sweep instead of clash refusal and stale GC (D5), off-loop directory I/O (D5), inbox re-check (D7), rate limit (D7), README serialization | Bearer peer token on the wire; startup refusal on a name clash |
| External prior art | skipped: the transport is plain signed HTTP between ccw instances; the open questions are ccw-internal | - | - |

## Research inputs

| Source | Entry | Lands in |
|---|---|---|
| brief section 5 (F8) | `CLAUDE_CODE_HARBOR_KITE` overrides the gate either way; deny rules block by tool name | peer-session-bootstrap |
| brief section 4 (F7) | reply goes to the sender's `from`; idle notice `{state, finished_at, detail}` after handling | peer-inbox-outcomes, peer-service-core |
| brief section 7 (F9) | fresh named volume is root 755; pre-create the mount point | peers-image-compose-docs |
| brief section 7 (F9) | AF_UNIX works only within one kernel | rejected: native transport dropped (D1) |
| brief section 1 (F1, F5) | native registry keyed by pid collides across containers | rejected: discovery is ccw's own directory keyed by session id (D5) |
| brief section 2 (F2) | explicit socket path and dir vetting | rejected: no sockets in the new design (D1) |
| brief section 4 (F6) | `crossSessionInbound: accept` for bypass receivers | rejected: native inbound not used (D1) |
| brief section 8 fix 2 | shared pid namespace | rejected: user decision D1 |
| brief section 8 fix 5 | ccw-side relay over HTTP through an MCP tool | peers-mcp-tools, peer-messages-endpoint, peer-http-client |
| mcp_server.py, internal_api.py | existing ccw MCP server and loopback route | peers-mcp-tools |
| router.py `_on_pre_tool_use` | `mcp__ccw__*` subagent deny already main-agent-only | peer-session-bootstrap |
| router.py `on_state`, `submit_turn` | ready trigger and turn submission with origin | peer-inbox-delivery |
| router.py `_on_stop` awaits `_after_turn` | network I/O there delays the Stop answer | peer-inbox-outcomes |
| plan review item 1 | HMAC request and entry signing | peer-auth-signing, peer-directory-module, peer-http-client, peer-messages-endpoint |
| plan review items 4 to 6 | sweep own dir, stale GC, name rules, off-loop I/O | peers-config-section, peer-directory-module |
| plan review items 7 to 9 | inbox re-check, rate limit, client retry | peer-inbox-delivery, peer-http-client |

## Decision coverage

| Decision | Tasks |
|---|---|
| D1 ccw-owned HTTP messaging | peer-http-client, peer-messages-endpoint, peer-service-core, manager-peers-lifecycle |
| D2 Opt-in peers config | peers-config-section, manager-peers-lifecycle |
| D3 Native built-ins off | peer-session-bootstrap, manager-peers-lifecycle |
| D4 MCP tools | peers-mcp-tools, peer-session-bootstrap, peer-service-core |
| D5 Discovery directory | peer-directory-module, peer-service-core, manager-peers-lifecycle |
| D6 Peer HTTP API and auth | peer-messages-endpoint, peer-http-client |
| D7 Inbox delivery | peer-inbox-delivery, peer-service-core |
| D8 Delivery notices | peer-inbox-outcomes, peer-service-core, peers-two-instance-e2e |
| D9 Events and metrics | peer-events-and-metrics, peer-inbox-delivery, peer-service-core |
| D10 Image and compose | peers-image-compose-docs |
| D11 README | peers-config-section, peer-events-and-metrics, peer-messages-endpoint, peers-readme-guide |
| D12 Tests and conventions | fake-claude-peer-harness, peers-two-instance-e2e, peers-two-container-test |
| D13 HMAC signing | peer-auth-signing, peer-directory-module, peer-http-client, peer-messages-endpoint |
| D14 Delivery guarantees and retries | peer-http-client, peer-inbox-delivery, peers-readme-guide |

## Contracts

| Part | Owner | Consumers | Where defined |
|---|---|---|---|
| `fake-match-substitution` | fake-claude-peer-harness | peers-two-instance-e2e, peers-two-container-test | `tests/fake_claude/fake_claude.py` |
| `ccw-service-fixture` | fake-claude-peer-harness | peers-two-instance-e2e | `tests/conftest.py` |
| `peers-config` | peers-config-section | peer-directory-module, peer-inbox-delivery, peer-messages-endpoint, peer-session-bootstrap, peer-service-core, manager-peers-lifecycle, peers-image-compose-docs | `src/claude_code_wrapper/config.py` |
| `peer-auth` | peer-auth-signing | peer-directory-module, peer-http-client, peer-messages-endpoint | `src/claude_code_wrapper/peer_auth.py` |
| `peer-directory-entry` | peer-directory-module | peer-http-client, peers-mcp-tools, peer-service-core | `src/claude_code_wrapper/peers.py` |
| `peer-events` | peer-events-and-metrics | peer-inbox-delivery, peer-service-core | `src/claude_code_wrapper/events.py` |
| `peer-metrics` | peer-events-and-metrics | peer-inbox-delivery, peer-messages-endpoint, peer-service-core | `src/claude_code_wrapper/metrics.py` |
| `peer-wire` | peer-http-client | peer-inbox-delivery, peer-messages-endpoint, peers-mcp-tools, peer-service-core | `src/claude_code_wrapper/peer_client.py` |
| `peer-inbox` | peer-inbox-delivery | peer-inbox-outcomes, peer-service-core, manager-peers-lifecycle | `src/claude_code_wrapper/peer_inbox.py` |
| `peer-inbox-outcome` | peer-inbox-outcomes | peer-service-core | `src/claude_code_wrapper/peer_inbox.py` |
| `peer-messages-endpoint` | peer-messages-endpoint | peer-service-core, manager-peers-lifecycle | `src/claude_code_wrapper/api/peers.py` |
| `ccw-mcp-peers-tool` | peers-mcp-tools | peer-service-core, manager-peers-lifecycle | `src/claude_code_wrapper/mcp_server.py` |
| `peer-session-bootstrap` | peer-session-bootstrap | manager-peers-lifecycle | `src/claude_code_wrapper/bootstrap/settings.py` |
| `peer-service` | peer-service-core | manager-peers-lifecycle | `src/claude_code_wrapper/peer_service.py` |
| `peers-image` | peers-image-compose-docs | peers-two-container-test | `Dockerfile` |

Cross-epic (docs/plans/contracts.json): this epic owns `peers-config`, `peer-directory-entry`,
`peer-messages-endpoint` and `ccw-mcp-peers-tool`, and consumes `ccw-mcp-tool` (owner
agent-callable-slash-command: `handle_message` becomes async with an optional `peers` argument,
the `command` tool unchanged) and `event-types` (owner hook-capabilities-for-tatara: three types
added).

## Review focus

- Zero change when off: argv, env, settings.json, .claude.json, CLAUDE.md, routes, MCP tool list
  and filesystem (peer-session-bootstrap, peers-mcp-tools, peer-messages-endpoint,
  manager-peers-lifecycle ACs).
- Signing (peer-auth-signing, peer-messages-endpoint, peer-directory-module): canonical strings,
  constant-time compares, skew window, bounded nonce cache, verification over raw bytes before
  parse, forged entries ignored, the token absent from every request and file.
- Delivery concurrency (peer-inbox-delivery): never submits while a turn or command runner is
  active, keeps arrival order, never strands an item, honours a kick that lands mid-attempt, does
  not race the idle command runner or idle auto-compact, and drops cleanly on close.
- Notices (peer-inbox-outcomes, peer-service-core): spawned off the Stop path, rejected and dropped
  always sent, notices never produce notices, shutdown drain bounded.
- Directory (peer-directory-module): off-loop I/O with timeouts, atomic writes, sweep and GC rules.
- `handle_message` going async must not change any `command` tool behaviour (ccw-mcp-tool).

## Left to the builder

- Internal structure of peer_auth.py, peers.py, peer_client.py, peer_inbox.py and peer_service.py
  within the interfaces stated in the tasks.
- The exact wording of the peers paragraph, the tool descriptions and the README prose, within the
  content listed in the tasks.
- Poll intervals in tests, within bounded deadlines.

## Assumptions and unknowns

- Real claude honours `CLAUDE_CODE_HARBOR_KITE=0` and the settings deny rules (F8, from the
  bundle); not testable without credentials. cannotVerify at run time; the user can confirm with
  one logged-in container (`ListAgents` absent from the tool list).
- Real claude accepts the `peers` and `send_message` input schemas; cannotVerify, same reason.
- Docker copies image directory ownership into a fresh named volume (F9); confirmed by the docker
  integration test.
- Nodes sharing an RWX directory have clocks within `max_clock_skew_seconds` of each other;
  documented as an operator requirement in the README.

## Open questions

none

## Irreversible operations in scope

none
