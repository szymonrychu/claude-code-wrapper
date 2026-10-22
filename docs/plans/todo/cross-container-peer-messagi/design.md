# Cross-container peer messaging over a shared mount design

## Summary

Main agents in different ccw sessions and containers message each other with Claude Code's native
peer messaging (SendMessage to a `uds:` socket address). An opt-in `peers:` config section (off by
default, zero change when off) makes ccw launch every session with `--messaging-socket-path
<peers.dir>/socks/<session id>.sock` on a shared mount, set `CLAUDE_CODE_HARBOR_KITE=1` and
`CLAUDE_CODE_SESSION_NAME`, and write `crossSessionInbound: accept`. Discovery is a ccw-owned peer
directory on the same mount plus a `peers` tool on the ccw MCP server; the image pre-creates `/peers`,
a compose example runs two peered containers, and a docker test proves cross-container discovery and
reachability with the fake claude.

## Intent and why

The user will run several ccw containers whose main agents must talk to each other. Today they
cannot: each session has a private `CLAUDE_CONFIG_DIR` registry, sockets sit in a container-private
`/tmp`, every container's first claude gets the same small pid so a shared native registry collides,
and ccw's bypass permission mode makes Claude hold every inbound peer message for a TUI approval that
nobody can give. The native transport itself already works between containers (facts F3, F9), so the
work is configuration, discovery and packaging, not a new transport.

## Goals

- `peers.enabled: false` (the default): argv, env, settings.json, .claude.json and CLAUDE.md are
  byte-identical to today, and no peers path is touched.
- `peers.enabled: true`: each session binds its messaging socket at `<dir>/socks/<session id>.sock`,
  has `CLAUDE_CODE_HARBOR_KITE=1`, `CLAUDE_CODE_SESSION_NAME=<instance>-<session name or id>` and
  `crossSessionInbound: accept`, and is published in `<dir>/directory/<session id>.json` until it stops.
- The ccw MCP `peers` tool lists live peers (connect probe) other than the caller with `uds:` addresses.
- ccw refuses to start with a clear error when the peers dir is insecure or not ours.
- `/peers` exists in the image as agent:agent 0700; a compose example runs two peered containers.
- A docker test shows a session in container A listing and reaching a session in container B.
- README documents setup, addressing, the same-host limit and the trust model; every new config key
  has a README row.

## Non-goals

- Shared pid namespace or native ListAgents discovery across containers.
- Mirroring Claude's own session registry (`<config>/sessions/<pid>.json`).
- Cross-host messaging, an HTTP relay, or a public HTTP peers endpoint.
- Testing delivery into a real claude (no credentials in tests).

## Anti-goals

- Any behaviour change for configs without `peers.enabled: true`.
- Claude silently falling back to its private `/tmp/cc-socks-<uid>` while ccw advertises a shared address.
- One ccw deleting a live peer's directory entry.
- Depending on Claude's undocumented on-disk registry format.

## Constraints

- Repo CLAUDE.md: test-first (red, green, refactor), always `uv run`, plain ASCII, no em dashes.
- Tests use the fake claude under tests/fake_claude only; only the fake-claude task
  (fake-claude-peer-socket) edits tests/conftest.py.
- Claude Code 2.1.304 in the image (Dockerfile `CLAUDE_CODE_VERSION`).
- Unix socket paths are at most 103 bytes (F2), validated at config load.
- Every config key needs a README row (tests/test_readme_config.py); examples/*.config.yaml are
  committed symlinks into src/claude_code_wrapper/data/examples.

## Facts (verified by research, carried from the scratch brief)

Source: reverse engineering of the claude 2.1.307 (macOS) and 2.1.304 (Linux, from the image)
bundles plus throwaway Docker experiments with `--network none`, no credentials.

- F1 Registry: Claude's native registry is `$CLAUDE_CONFIG_DIR/sessions/<pid>.json` (follows
  CLAUDE_CONFIG_DIR; verified in a container). The file name is the pid, there is no uniqueness check,
  status updates read-modify-write it and exit unlinks it. `pidDomain` is `linux:<machine-id>:<pid ns>`;
  a foreign domain is listed only if its socket accepts a connect (250 ms probe). Sweeping is declined
  inside Docker.
- F2 Socket path: default `$XDG_RUNTIME_DIR/cc-socks/<pid>.sock`, else `$CLAUDE_CODE_TMPDIR`, else
  `/tmp/cc-socks`; limit 103 bytes. `--messaging-socket-path <path>` must be absolute, without `..`,
  at most 103 bytes, in a parent dir owned by the uid with mode 0700, and not already live. Every
  ancestor must be owned by us or root and be private or sticky; the leaf is chmod'ed 0700 and the
  socket 0600. A refusal falls back to `/tmp/cc-socks-<uid>` with the error text "A directory on the
  sockets path is shared (world- or group-writable without the sticky bit, e.g. a container volume
  mounted at /tmp)".
- F3 Wire: NDJSON over AF_UNIX, one JSON object per line (max 1048576 chars), first line within 30 s.
  An optional first `{"type":"auth","token":...}` line; auth is required only on Windows, so on Linux
  an unauthenticated line is accepted. User message:
  `{"type":"user","message":{"role":"user","content":"..."},"priority":"next","from":"uds:<sender socket>",...}`.
- F4 Liveness: Claude's own listing probes each socket with a 250 ms connect; ccw mirrors that default.
- F5 Pid collision: two containers sharing one native registry both ran claude as pid 10; the socket
  collision was handled (`10-<8hex>.sock`) but the registry kept one `10.json` (A's was overwritten).
  This is why discovery is a ccw directory keyed by ccw's unique session id.
- F6 Inbound policy: setting `crossSessionInbound` = accept | hold | refuse, read from user settings
  (an explicit value wins). Unset, a bypass-mode receiver of a message with no asserted mode holds it
  for a TUI approval ("no-mode-asserted") and headless it expires. ccw runs bypassPermissions, so it
  must write `accept`.
- F7 Replies and receipts: a reply is a new SendMessage to the sender's `from` address, which must be
  reachable from the receiver's filesystem. Receipts and `notify_when_idle` are vetted: same directory
  as the receiver's own socket is accepted; otherwise a kernel-verified peer pid is needed, which is
  0 across pid namespaces. With every socket in one shared dir, replies, receipts and idle notices work.
- F8 Gating: `CLAUDE_CODE_HARBOR_KITE` overrides the GrowthBook gate `tengu_harbor_kite` (default
  true) either way; pin it to 1. `--bare`/`CLAUDE_CODE_SIMPLE` skip messaging unless
  `--messaging-socket-path` is given. No gate on TTY, login, `IS_SANDBOX` or skip-permissions.
  `CLAUDE_CODE_SESSION_NAME` sets a stable display name (derived names are `<cwd basename>-<2 hex>`).
- F9 Docker: a Python AF_UNIX server in one container and a client in another connected through a
  named volume and through a host bind mount (Docker Desktop, linuxkit kernel); SO_PEERCRED reports
  pid 0 across namespaces. A fresh named volume is root:root 755 (uid 1000 gets EACCES) unless the
  image pre-creates the mount point owned by the user, which Docker copies into a new volume.
- F10 ccw today: per-session `CLAUDE_CONFIG_DIR=/sessions/<id>/config`, env built from an allow-list
  (session.py `build_env`), argv `claude [--model] [--effort] --session-id|--resume <uuid> [extra_args]`
  (session.py `_argv`), session ids `ses_` plus 12 hex (manager.py `_new_session_id`), uid 1000
  `agent`, ccw is PID 1. The ccw MCP server (mcp_server.py) exposes one `command` tool on the
  loopback internal API.

## Decisions

### D1: Discovery is a ccw peer directory on the shared mount plus a ccw `peers` tool

User decision: shared mount plus ccw peers tool (research fixes 1 and 3). Rejected: shared pid
namespace (couples container lifecycles and lets containers signal each other, single owner process
needed), mirroring Claude's registry under synthetic pids (undocumented format, brittle across
versions), HTTP relay (not native SendMessage). Serves A.

### D2: Opt-in `peers:` config section

`peers: {enabled: false, dir: /peers, name: "", probe_timeout_ms: 250}`. `dir` is absolute, the same
path in every container, and `len("<dir>/socks/<16-char session id>.sock")` must be at most 103 bytes,
checked at config load. `name` is the instance name, empty meaning the host name at runtime, limited to
`[A-Za-z0-9._-]`, at most 64 chars (my call). With peers on, `--messaging-socket-path` in
`claude.extra_args` and a user `claude.mcp_servers.ccw` are config errors (my call). Serves B.

### D3: Per-session claude wiring

When enabled, per session: argv gets `--messaging-socket-path <dir>/socks/<session id>.sock` right
after the session id (create and resume); env gets `CLAUDE_CODE_HARBOR_KITE=1` and
`CLAUDE_CODE_SESSION_NAME=<instance>-<session name or id>` (characters outside `[A-Za-z0-9._-]`
replaced by `-`, my call), layered over user env so ccw's values win (my call); the ccw-written user
settings.json gets `crossSessionInbound: accept` (an explicit `claude.settings` overlay still wins,
my call) and the allow rule `mcp__ccw__peers`. Turning peers off and resuming removes a ccw-written
`accept` (my call). Serves C.

### D4: Peers dir preparation and vetting at startup

At manager construction ccw creates `<dir>` (if missing), `<dir>/socks` and `<dir>/directory` with
0700, owned by the service uid, and refuses to start (service exits 1, `ccw: peers: ...`) when the
dir or a subdir is not ours, or the dir or an ancestor is group- or world-writable without the sticky
bit, or an ancestor is owned by neither us nor root. This mirrors Claude's own vetting (F2) so Claude
never silently falls back to `/tmp/cc-socks-<uid>`. Ancestor checks are my call, taken from F2.
Serves D.

### D5: Peer directory entries and liveness

ccw writes `<dir>/directory/<session id>.json` atomically (temp file plus rename, 0600) after a
session's onCreate/onResume gate passes: `{session_id, name, instance, address:
"uds:<dir>/socks/<session id>.sock", cwd, started_at}`, `name` being the CLAUDE_CODE_SESSION_NAME
value (my call). It removes the entry and the socket file in `_teardown`, which every stop path
(destroy, failed or vetoed create, retire, abandon, shutdown) goes through. Liveness is an AF_UNIX
connect probe with `probe_timeout_ms`, run concurrently. Sweeping: listing only hides dead entries;
`prepare()` at startup deletes dead entries of its own instance only (my call: a peer whose claude is
restarting briefly refuses connects, and deleting another instance's entry would hide it for good).
Serves E.

### D6: ccw MCP `peers` tool and agent instructions

The ccw MCP server gets a second tool `peers` (no required params, optional `instance` filter)
returning live peers excluding the caller, each with its `uds:` address and a one-line SendMessage
hint, as compact JSON text. It is listed only when peers are enabled; when enabled the ccw MCP server
is injected even with an empty agent allowlist. `handle_message` becomes async so the concurrent
probes never block the event loop (my call). The CLAUDE.md that ccw writes gets one paragraph
explaining the tool, SendMessage and `<agent-message>` replies when enabled. Serves F.

### D7: Image and compose example

The Dockerfile pre-creates `/peers` owned by agent (uid 1000) mode 0700 so a fresh named volume
inherits it (F9). `examples/docker-compose.peers.yml` runs `ccw-a` and `ccw-b` sharing one named
`peers` volume at `/peers`, each with its own state volumes and loopback port, from a peers example
config that is a committed symlink into the package data. Serves G.

### D8: Tests and conventions

The fake claude accepts `--messaging-socket-path`, binds an AF_UNIX listener there before its idle
footer and records received lines in `$CLAUDE_CONFIG_DIR/fake-peer-inbox.jsonl`; tests/conftest.py
gets a short `/tmp` `peers_dir` fixture (macOS pytest tmp paths are too long for 103 bytes).
In-process tests run two managers on one peers dir. A docker-marked integration test
(`mise run test:integration:peers`, also run by `mise run test:integration` and so by CI, my call)
starts two containers on one peers volume, creates a session in each through the API, shows A's
`peers` tool listing B and a connect from container A to B's socket delivering a line. Real-claude
delivery cannot be verified without credentials and is recorded as cannotVerify, not as a task.
All work follows the repo conventions (test-first, `uv run`, ASCII). Serves H and J.

### D9: README

A `## Peer messaging` section (setup, addressing, what ccw sets, same-host-only limit including the
Kubernetes note, trust model), a pointer from `## Trust model`, the compose example in
`## Docker and compose`, config rows for every `peers.*` key, and the `peers` tool under
`### POST /internal/mcp`. Serves I.

## Research

| Lens | Ran or skipped | Adopted | Rejected |
|---|---|---|---|
| Claude Code bundle reverse engineering | ran (2.1.304 Linux, 2.1.307 macOS; scratch brief) | explicit `--messaging-socket-path` (F2), `crossSessionInbound` accept (F6), `CLAUDE_CODE_HARBOR_KITE=1` and `CLAUDE_CODE_SESSION_NAME` (F8), 250 ms probe (F4) | relying on the native registry (F1, F5) |
| Docker experiments | ran (named volume and bind mount, two containers) | shared named volume at one path, image-created `/peers` (F9) | shared pid namespace (user decision) |
| ccw codebase | ran (session.py, manager.py, mcp_server.py, bootstrap/, Dockerfile, compose) | reuse the ccw MCP server, settings render and build_env layering (F10) | a new HTTP endpoint |
| External prior art | skipped: the feature is specific to Claude Code's undocumented transport; the bundle is the source | - | - |

## Research inputs

| Source | Entry | Lands in |
|---|---|---|
| brief section 2 (F2) | explicit socket path, 103-byte limit, dir vetting | peers-config-section, peer-directory-module, peer-session-bootstrap |
| brief section 4 (F6) | bypass receivers hold peer messages without `crossSessionInbound: accept` | peer-session-bootstrap |
| brief section 5 (F8) | pin `CLAUDE_CODE_HARBOR_KITE=1`, set `CLAUDE_CODE_SESSION_NAME` | peer-session-bootstrap, manager-peers-lifecycle |
| brief section 1 (F1, F4) | foreign-domain listing by connect probe, 250 ms | peer-directory-module |
| brief section 8 (F5) | native registry collides on pids across containers | rejected: discovery is ccw's own directory (D1) |
| brief section 8 fix 2 | shared pid namespace | rejected: user decision D1 |
| brief section 8 fix 4 | registry federation with synthetic pids | rejected: brittle undocumented format (D1) |
| brief section 8 fix 5 | HTTP relay | rejected: not native SendMessage, user decision D1 |
| brief section 7 (F9) | fresh named volume is root 755; pre-create `/peers` | peers-image-compose-and-docs |
| brief section 4 (F7) | replies, receipts, idle notices work with one shared socket dir | peers-image-compose-and-docs (README) |
| brief section 3 (F3) | NDJSON wire, auth optional on Linux | fake-claude-peer-socket, peers-two-container-test, peers-image-compose-and-docs (trust model) |
| mcp_server.py, internal_api.py | existing ccw MCP server and route | peers-mcp-tool |

## Decision coverage

| Decision | Tasks |
|---|---|
| D1 Discovery via ccw directory and peers tool | peer-directory-module, peers-mcp-tool, manager-peers-lifecycle |
| D2 Opt-in peers config | peers-config-section, manager-peers-lifecycle |
| D3 Per-session claude wiring | peer-session-bootstrap, manager-peers-lifecycle |
| D4 Dir preparation and vetting | peer-directory-module, manager-peers-lifecycle |
| D5 Directory entries and liveness | peer-directory-module, manager-peers-lifecycle |
| D6 MCP peers tool and instructions | peers-mcp-tool, peer-session-bootstrap, manager-peers-lifecycle |
| D7 Image and compose example | peers-image-compose-and-docs |
| D8 Tests and conventions | fake-claude-peer-socket, manager-peers-lifecycle, peers-two-container-test |
| D9 README | peers-config-section, peers-mcp-tool, peers-image-compose-and-docs |

## Contracts

| Part | Owner | Consumers | Where defined |
|---|---|---|---|
| `fake-peer-socket` | fake-claude-peer-socket | manager-peers-lifecycle, peers-two-container-test | `tests/fake_claude/fake_claude.py` |
| `peers-dir-fixture` | fake-claude-peer-socket | peer-directory-module, peers-mcp-tool, manager-peers-lifecycle | `tests/conftest.py` |
| `peers-config` | peers-config-section | peer-directory-module, manager-peers-lifecycle, peers-image-compose-and-docs | `src/claude_code_wrapper/config.py` |
| `peer-directory` | peer-directory-module | peers-mcp-tool, manager-peers-lifecycle | `src/claude_code_wrapper/peers.py` |
| `peer-session-bootstrap` | peer-session-bootstrap | manager-peers-lifecycle | `src/claude_code_wrapper/session.py` |
| `ccw-mcp-peers-tool` | peers-mcp-tool | manager-peers-lifecycle | `src/claude_code_wrapper/mcp_server.py` |
| `peers-image` | peers-image-compose-and-docs | peers-two-container-test | `Dockerfile` |

Cross-epic (docs/plans/contracts.json): this epic owns `peers-config`, `peer-directory-entry` and
`ccw-mcp-peers-tool`, and consumes `ccw-mcp-tool` (owner agent-callable-slash-command; the `command`
tool stays unchanged, `handle_message` becomes async with an optional `peers` argument).

## Review focus

- Zero change when off: argv, env, settings.json, .claude.json, CLAUDE.md and the filesystem
  (manager-peers-lifecycle AC4, peer-session-bootstrap AC1, AC2, AC4).
- Dir vetting matches Claude's rules closely enough that Claude never falls back silently (D4, F2).
- Every stop path removes the entry and socket (`_teardown`), and nothing deletes another instance's
  entry (D5).
- `handle_message` going async must not change any `command` tool behaviour (ccw-mcp-tool contract).
- Trust model wording in README: prompt injection into bypass-mode agents by anything with the uid.

## Left to the builder

- Internal structure of peers.py (helpers, dataclass vs dict for entries) within the stated interface.
- The exact wording of PEERS_INSTRUCTIONS, PEERS_DESCRIPTION and the README prose, within the content
  listed in the tasks.
- How the docker test waits (poll interval) within bounded deadlines.

## Assumptions and unknowns

- Real claude delivery end to end (SendMessage from one real claude to another across containers) is
  not testable without credentials; F3, F6 and F9 come from the bundle and the Docker experiments.
  Recorded as cannotVerify at run time; the user can confirm with two logged-in containers.
- Claude 2.1.304 accepts `--messaging-socket-path` in interactive mode (F2, F8 from the bundle); the
  flag is not exercised by the fake in a way that proves Claude's parsing. Confirmed only by a real run.
- Docker copies image directory ownership into a fresh named volume (standard Docker behaviour, F9);
  confirmed by the docker integration test.

## Open questions

none

## Irreversible operations in scope

none
