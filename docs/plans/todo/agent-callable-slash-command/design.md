# Agent-callable slash commands and idle auto-compact design

## Summary

ccw serves an MCP server (`ccw`) over HTTP on the per-session internal API. Its one tool,
`command`, queues an allowlisted Claude Code slash command (`/compact [instructions]`,
`/reload-plugins`, `/reload-skills`, `/mcp reconnect`, `/context`). At the turn's Stop ccw holds the turn (defer leg), types the command at the idle
prompt, waits for its completion signal, and pastes a continuation prompt with the same turn marker
so the agent resumes in the same turn. Clients queue the same commands through
`POST /v1/sessions/{id}/commands`. An optional idle timer queues `/compact`. The transcript parser
stops treating compact summaries and command echoes as prompts.

Prefix C/ = `src/claude_code_wrapper`. main at 4b1d358 (hooks epic K1-K13 merged up to 18/20
tasks); installed claude 2.1.306, image pins 2.1.304.

## Intent and why

Long-lived agents run for hours. They need to compact their own context and pick up plugin changes
without a human typing slash commands. ccw already owns the PTY, so it can type them safely, but
only at an idle prompt and without losing turn identity.

## Goals

- Main agent can self-compact, self-reload plugins and run the other allowlisted commands through
  the one MCP tool `mcp__ccw__command`.
- Same-turn resume after the command (user decision).
- Public endpoint for clients (user decision).
- Optional idle auto-compact.
- Events, hooks, metrics, README.
- Stale-marker fix in the transcript parser.

## Non-goals

- Session-replacing, dialog or skill commands; auto `--force`; subagent or teammate callers (user
  decision: main agent only); changing Claude Code's own idleCompaction.

## Anti-goals

- Never type into the PTY while claude is responding or while another write is in flight.
- Never write the internal token into a world-readable file.

## Constraints

- TDD; fake claude only in unit tests (`tests/fake_claude/fake_claude.py`), extended to handle
  slash commands (echo entries, compact boundary, PostCompact hook, reload stdout).
- Defaults off/unchanged when `sessions.commands` and `sessions.auto_compact` are not configured,
  except the parser fix, which is always on.
- `mise run lint`, `mise run test` green, plain ASCII.

## Facts (verified by research, file:line or source)

- PTY typing is paste then CR, single writer (C/pty_proc.py:223). Turn identity is the marker only
  (C/session.py:771-774, C/router.py:666-668).
- The defer leg answers allow while keeping the turn running (C/router.py:532-534); `_stop_gate`
  returns early unless an onStop hook or defer_stop_on_background_tasks is set (C/router.py:503).
- Held-turn continuation exists (C/session.py:648-669, K12).
- stop_target drops Stops whose session_id differs from the spawned one (C/session.py:872), so
  `/clear` would hang every later turn: excluded.
- The relay covers 7 hook events (C/hook_relay.py:23-31); render_settings loops over them
  (C/bootstrap/settings.py:93). Internal API: loopback + constant-time token (C/internal_api.py:55-63);
  token regenerated per activation (C/manager.py:1101).
- seed_claude_json already writes mcpServers (C/bootstrap/claude_json.py:86-101) on create and
  resume (C/manager.py:493-519).
- Transcript parser drops `system` lines (C/transcript.py:179-181) and turns every user string,
  compact summaries included, into UserPrompt and extracts a marker (C/transcript.py:146-158).
  Compact summaries quote "All user messages", so an old `ccw turn` marker can resurface.
- Reaper stops only `ready` sessions, idleness from the last event ts (C/manager.py:997-1011,
  1227-1230).
- Upstream (https://code.claude.com/docs/en/commands, /hooks, /mcp, settings-reference
  idleCompaction; changelog; 2.1.306 binary strings):
  - `/compact [instructions]`; `/reload-plugins [--force]` skips with "Run /reload-plugins --force
    to apply." when the MCP tool set would change; `/reload-skills`; `/mcp reconnect (<server>|all)`;
    `/context`. Commands sent while responding are queued by claude itself.
  - PreCompact `{trigger, custom_instructions}` (can block); PostCompact `{trigger,
    compact_summary}` (2.1.76); SessionStart source `compact`. No plugin-reload hook.
  - Transcript: `{"type":"system","subtype":"compact_boundary","compactMetadata":{"trigger":
    "manual","preTokens":...,"postTokens":...,"durationMs":...}}`, same sessionId; a command shows
    as a user entry `<command-name>/X</command-name><command-args>..` followed by
    `<local-command-stdout>...` as a system/local_command or user entry. Compact failures:
    "Compaction canceled.", "Not enough messages to compact.", "Error compacting conversation".
    Reload success: stdout starting "Reloaded: ".
  - MCP: `${VAR}` expansion in url and headers of claude.json entries; HTTP servers have a 60s
    per-request timeout; `alwaysLoad` exempts from tool-search deferral; server `instructions` load
    at start.
  - Claude Code's own idle compaction (about 54 min, >= 200k tokens, feature-flagged) stays as is
    and is reported through the same `compacted` event.
- Probed by the planner (2026-10-21) with `claude mcp list` against a local listener, a config
  dir whose `.claude.json` held `mcpServers.ccw = {type: http, url: "${PROBE_URL}/internal/mcp",
  headers: {"X-CCW-Hook-Token": "${PROBE_TOKEN}"}}`:
  - claude 2.1.304 (the image pin) and 2.1.306 both expand `${VAR}` in the url and in header
    values: the request reached the listener with `x-ccw-hook-token: <the env value>`. R1 is
    settled; no literal-token fallback is built.
  - Handshake on 2.1.306: `POST {"method": "server/discover", "id": ...}` first (header
    `mcp-protocol-version: 2026-07-28`); answered with JSON-RPC -32601 it falls back to
    `initialize` (client protocolVersion `2025-11-25`; a server answer of `2025-06-18` is
    accepted), then `notifications/initialized` (no id; 202 accepted), then a `GET` on the url
    (405 accepted), then `tools/list`. `claude mcp list` reported "Connected".
- Every config dir is per session (`_Active.config_dir`), so claude.json and settings.json can
  carry the session's own allowlist. `CCW_INTERNAL_URL`, `CCW_SESSION_ID` and `CCW_HOOK_TOKEN`
  are already in claude's env (C/session.py:244-245, C/hook_relay.py:37-39).
- `examples/*.config.yaml` are symlinks to `src/claude_code_wrapper/data/examples/` (git mode
  120000): only the data copies are edited.
- README pins: tests/test_readme_config.py requires every config key in the README tables;
  tests/test_readme_api.py requires every public and internal route, event type, hook event and
  error code to have its section. tests/test_config.py pins `len(HOOK_EVENTS) == 14`,
  `HOOK_EVENTS[-1] == "onError"` and `VETO_EVENTS`; tests/test_hook_relay.py::test_constants pins
  `CLAUDE_HOOK_EVENTS`; tests/test_event_log.py::test_event_types_exact pins `EVENT_TYPES`.
- The World fixture's loopback server (tests/conftest.py:92-113) serves only
  `/internal/hooks/{event}`; `boot.idle_ready_seconds` is 30 there, so a quiet-screen wait tied to
  it would make every command test wait 30 s.

## Decisions

### D1: Agent interface is a ccw MCP server over HTTP on the internal API
`POST /internal/mcp`, JSON-RPC 2.0 (initialize, notifications -> 202, tools/list, tools/call,
ping; `server/discover` and every other method -> -32601, probed as the 2.1.30x first request;
JSON responses; GET and DELETE 405), authenticated like the hook relay: loopback only,
`X-CCW-Session` plus `X-CCW-Hook-Token` compared in constant time. Injected into the session's
claude.json `mcpServers.ccw` as `{type: "http", url: "${CCW_INTERNAL_URL}/internal/mcp", headers:
{"X-CCW-Session": "${CCW_SESSION_ID}", "X-CCW-Hook-Token": "${CCW_HOOK_TOKEN}"}, alwaysLoad: true}`
so the token is never written to a file; expansion was probed on 2.1.304 and 2.1.306 (Facts), so
no literal-token fallback is built. Injected only when the session's agent allowlist is non-empty;
an empty list removes a previous `ccw` entry and the allow rule. An operator `claude.mcp_servers`
entry named `ccw` is a config error while `sessions.commands.agent` is non-empty (my call: the name
is reserved). Server `instructions` explain the tool. ONE tool (user decision): `command` with
input schema `{name: enum of the session's agent allowlist, params?: object}`; params are optional
and per command (compact: `instructions?: string`; mcp: `action: "reconnect"`, `server?: string`
default all; reload-plugins: `force?: bool`, honoured only when allow_force; reload-skills and
context: none). The schema documents each command's params (oneOf per name or a described object);
unknown params are rejected with a tool error naming the allowed ones. settings.json allow gets
`mcp__ccw__command`; operators restrict commands through the allowlist, not per tool. The public
endpoint (D5) is deliberately redundant with the turns endpoint for clients (user accepted).
Alternatives: CLI via Bash (dead when Bash is denied, opaque to gates), PreToolUse pseudo tool
(rejected). Serves approval A. (research pick)

### D2: Main agent only
A call whose hook context carries an agent_id (subagent or teammate; known from the PreToolUse relay
for `mcp__ccw__*`, which ccw sees) is refused with a clear tool error: the router denies that
PreToolUse before any user hook, so the MCP call never happens and no command is queued. User
decision. Serves approval A.

### D3: Command queue and allowlist
`router.request_command(name, args, origin=agent|client|idle)`. Built-in set {compact,
reload-plugins, reload-skills, mcp (reconnect only), context}; everything else is rejected at config
validation. `sessions.commands: {agent: [compact, reload-plugins], client: [compact, reload-plugins,
reload-skills, mcp, context], timeout_seconds: {compact: 600, default: 60}, continue_prompt: <text>,
allow_force: false, quiet_seconds: 1.5}`, overridable per session on CreateSessionRequest (stored
in the session meta, reapplied on resume). Both lists default to empty, so nothing changes until an
operator configures them. `quiet_seconds` is the quiet-screen wait before typing (my call: its own
key rather than `boot.idle_ready_seconds`, which tunes boot and is 30 s in the test World).
`origin: idle` is allowed `compact` only and bypasses both lists. Compact instructions are one line
(no control characters or newlines, at most 2000 characters) because they are typed into the PTY
(my call). Coalescing: a duplicate name
already queued returns the existing id (compact keeps the first instructions). Queue cap 4. Each
command gets a record {id, name, args, origin, state queued|running|completed|failed|cancelled,
result, error, timestamps}. `--force` only when allow_force. The agent tool returns success:
"Queued <id>. It runs when you end this turn; end your turn now with a one-line status. You will be
resumed automatically." Serves approvals A and B.

### D4: Same-turn lifecycle (user decision)
1. Request: validate, queue, `command_queued` event.
2. Stop: `_stop_gate` also gates when commands are pending. onStop block wins (command stays
   queued). Background tasks pending: defer as today, stays queued. Else answer allow, emit
   `turn_deferred{reason: "command"}`, start the runner after the relay response is sent.
3. Runner: wait for a quiet screen (no PTY output for `quiet_seconds`); fire `onCommand` (blocking, can
   deny: add to VETO_EVENTS; deny -> failed `vetoed`); type `/name args` with no marker; wait for the
   completion signal or timeout. compact: PostCompact(manual) relay or the compact_boundary entry =
   success, the failure stdout patterns = failure. Others: first local-command stdout/stderr after
   its command-name entry; reload-plugins "--force to apply" = result `skipped_cache`. Timeout: ESC
   once, failed `timeout`. Emit `command_completed` / `command_failed`, fire `onCommandComplete`.
   Next queued command.
4. Continue: queue empty -> `Session.continue_turn(record, text)` (factored from _continue_held,
   C/session.py:648-669) with `continue_prompt` plus a results line; the next Stop completes the
   same record; usage keeps accumulating; `turn_resumed{cause: "command"}`.
5. Interrupt: queued commands of the turn -> cancelled; running -> ESC, turn ends interrupted, no
   continuation.
6. Crash: command failed `claude_exited`; K12 decides the turn.
7. turn_max_seconds keeps counting (documented).
8. Idle session (no turn): `Session.begin_command()` marks busy so client turns get 409 busy and the
   reaper skips; runner steps 3 without continuation.

The runner is split across three tasks (my call, for size): the idle path and completion
(command-runner), failure handling (command-runner-failures), the turn integration
(command-same-turn-resume). Serves approval B.

### D5: Public endpoint (user decision)
`POST /v1/sessions/{id}/commands {name, args?}` -> 202 with the record (client allowlist, origin
`client`); `GET /v1/sessions/{id}/commands/{command_id}` and `GET /v1/sessions/{id}/commands`
(list). Same queue and lifecycle as the agent tool: queued during a turn, the command runs at that
turn's Stop and the turn resumes. Rejections map to error codes (my call): not in the client list
403 `command_not_allowed`, bad args 422 `invalid_command`, queue full 429 `command_queue_full`,
unknown command id 404 `command_not_found`, session not running 409 as for turns. Serves approval C.

### D6: Idle auto-compact
`sessions.auto_compact: {idle_seconds: 0 (off), instructions: "", min_context_tokens: 0}`. Idle =
state ready, no turn, no command, no event for idle_seconds. Armed when a turn ends, disarmed after
firing (once per idle gap). Skipped below min_context_tokens (last main-chain usage input + cache).
Reaper clock ignores events with origin idle; validator error when idle_seconds >=
idle_timeout_seconds > 0. Exit policies unaffected; shutdown abandons a running command.
"Events with origin idle" (my call on the mechanism): the command_* events of an idle-origin
command, and every event emitted between its command_started and its command_completed or
command_failed. Serves approval D.

### D7: Events, hooks, metrics
Events: command_queued, command_started, command_completed, command_failed, compacted{trigger,
pre_tokens, post_tokens, duration_ms} (for every compaction, including claude's own auto/idle),
plus turn_resumed cause. Hooks: onCommand (veto), onCommandComplete. Relay PreCompact and PostCompact
(new relay events). Metrics: ccw_commands_total{command,origin,result},
ccw_command_duration_seconds{command}, ccw_compactions_total{trigger}. Update
tests/test_event_log.py pin and README `### <event>` sections. Order (my call): the five events go
between `hook_result` and `error` in EVENT_TYPES; onCommand and onCommandComplete go before
onError in HOOK_EVENTS; PreCompact and PostCompact are appended to CLAUDE_HOOK_EVENTS. `compacted`
comes only from the transcript `compact_boundary` entry, so one compaction is one event even when
the PostCompact relay also arrives. Serves approval B.

### D8: Transcript parser fix (always on)
parse_line skips user entries with isCompactSummary, isMeta, `<command-name>` or
`<local-command-*>` content as prompts; adds CompactBoundary and CommandOutput events for system
lines. Prevents stale markers from compact summaries making Stops look stale. A user string is a
command echo only when it starts (after whitespace) with `<command-name>` or `<command-message>`,
and command output only when it starts with `<local-command-`, so a real prompt that merely
mentions the tags stays a prompt (my call). A `CommandInvoked{name, args}` event carries the echo,
because the runner matches a command's output to its echo. Serves approval E.

### D9: Examples and docs
README section "Slash commands" (tool, endpoint, lifecycle, config, auto-compact, limits); example
config snippet in data/examples; tatara and magic-trainer examples get an auto_compact block. The
snippet goes into `src/claude_code_wrapper/data/examples/workbench.config.yaml` and
`config.example.yaml` (my call); `examples/` holds symlinks, so only the data copies change.
Serves approval F.

## Research

| Lens | Ran / skipped | Adopted | Rejected |
|---|---|---|---|
| research-code-internal | ran (architect-researcher over C/ at 4b1d358) | defer + continue reuse (C/router.py:532-534, C/session.py:648-669), internal API auth (C/internal_api.py:55-63), seed_claude_json mcpServers (C/bootstrap/claude_json.py:86-101) | new process for MCP |
| research-docs | ran (code.claude.com docs commands, hooks, mcp, settings-reference; changelog; binary strings 2.1.306) plus the planner's probe of 2.1.304 and 2.1.306 (Facts) | completion signals, header expansion, allowlist, server/discover -> -32601 fallback | /clear, auto --force, literal-token fallback |
| research-code-external | skipped: no external analogue needed beyond MCP spec covered by docs | | |
| research-papers | skipped: no algorithmic core | | |

## Research inputs

| Source | Entry | Lands in |
|---|---|---|
| architect-researcher | D1 MCP server over HTTP on the internal API | ccw-mcp-endpoint, ccw-mcp-injection-and-e2e |
| architect-researcher | D2 main agent only | command-registry-and-config, ccw-mcp-injection-and-e2e |
| architect-researcher | D3 queue, allowlist, coalescing, cap | command-registry-and-config, command-runner |
| architect-researcher | D4 same-turn lifecycle | command-runner, command-runner-failures, command-same-turn-resume |
| architect-researcher | D5 public endpoint | commands-public-endpoint |
| architect-researcher | D6 idle auto-compact | idle-auto-compact |
| architect-researcher | D7 events, hooks, metrics | command-event-vocabulary, command-completion-signals, command-runner |
| architect-researcher | D8 transcript parser fix | transcript-command-entries |
| architect-researcher | D9 examples and docs | slash-commands-docs |
| research-docs | completion signals (PostCompact, compact_boundary, local-command stdout, failure strings) | fake-claude-slash-commands, transcript-command-entries, command-completion-signals, command-runner |
| research-docs | `${VAR}` expansion in MCP url and headers | ccw-mcp-injection-and-e2e |
| planner probe | 2.1.30x handshake starts with server/discover | fake-claude-mcp-step, ccw-mcp-endpoint |
| reuse | defer leg and held-turn continuation, C/router.py:_stop_gate, C/session.py:_continue_held | command-same-turn-resume |
| reuse | internal API auth, C/internal_api.py:create_internal_app, C/manager.py:hook_token_matches | ccw-mcp-endpoint |
| reuse | mcpServers writer, C/bootstrap/claude_json.py:seed_claude_json | ccw-mcp-injection-and-e2e |
| reuse | relay event loop, C/bootstrap/settings.py:render_settings | command-completion-signals |
| do-not-build | literal-token fallback in claude.json (probe shows env expansion works on the pin) | rejected: not needed |
| do-not-build | a separate MCP process | rejected: the internal API already has auth and the session lookup |

## Decision coverage

| Decision | Tasks |
|---|---|
| D1 Agent interface is a ccw MCP server | fake-claude-mcp-step, command-registry-and-config, ccw-mcp-endpoint, ccw-mcp-injection-and-e2e |
| D2 Main agent only | command-registry-and-config, ccw-mcp-injection-and-e2e |
| D3 Command queue and allowlist | command-registry-and-config, command-runner |
| D4 Same-turn lifecycle | fake-claude-slash-commands, command-completion-signals, command-runner, command-runner-failures, command-same-turn-resume |
| D5 Public endpoint | commands-public-endpoint |
| D6 Idle auto-compact | idle-auto-compact |
| D7 Events, hooks, metrics | command-event-vocabulary, command-completion-signals, command-runner |
| D8 Transcript parser fix | transcript-command-entries |
| D9 Examples and docs | slash-commands-docs |

## Contracts

| Part | Owner | Consumers | Where defined |
|---|---|---|---|
| `fake-claude-commands` | fake-claude-slash-commands | fake-claude-mcp-step, command-completion-signals, command-runner, command-runner-failures, command-same-turn-resume, idle-auto-compact | `tests/fake_claude/fake_claude.py` |
| `fake-claude-mcp` | fake-claude-mcp-step | ccw-mcp-injection-and-e2e | `tests/fake_claude/fake_claude.py` |
| `world-internal-forward` | fake-claude-mcp-step | ccw-mcp-injection-and-e2e | `tests/conftest.py` |
| `transcript-command-events` | transcript-command-entries | command-completion-signals | `src/claude_code_wrapper/transcript.py` |
| `command-queue` | command-registry-and-config | command-runner, ccw-mcp-endpoint, commands-public-endpoint | `src/claude_code_wrapper/commands.py` |
| `commands-config` | command-registry-and-config | command-runner, ccw-mcp-endpoint, ccw-mcp-injection-and-e2e, commands-public-endpoint, idle-auto-compact, slash-commands-docs | `src/claude_code_wrapper/config.py` |
| `command-events` | command-event-vocabulary | command-completion-signals, command-runner, command-same-turn-resume | `src/claude_code_wrapper/events.py` |
| `command-hooks` | command-event-vocabulary | command-runner | `src/claude_code_wrapper/config.py` |
| `command-metrics` | command-event-vocabulary | command-completion-signals, command-runner | `src/claude_code_wrapper/metrics.py` |
| `compact-relay-events` | command-completion-signals | command-runner | `src/claude_code_wrapper/hook_relay.py` |
| `command-signals` | command-completion-signals | command-runner | `src/claude_code_wrapper/command_signals.py` |
| `command-request-api` | command-runner | command-runner-failures, command-same-turn-resume, ccw-mcp-endpoint, commands-public-endpoint, idle-auto-compact | `src/claude_code_wrapper/router.py` |
| `continue-turn` | command-same-turn-resume | ccw-mcp-injection-and-e2e | `src/claude_code_wrapper/session.py` |
| `ccw-mcp-server` | ccw-mcp-endpoint | ccw-mcp-injection-and-e2e | `src/claude_code_wrapper/mcp_server.py` |
| `auto-compact-config` | idle-auto-compact | slash-commands-docs | `src/claude_code_wrapper/config.py` |

Cross-epic registry (`docs/plans/contracts.json`): this epic owns `command-request-api`
(router.py), `ccw-mcp-tool` (mcp_server.py), `commands-endpoint` (api/commands.py) and
`claude-relay-events` (hook_relay.py), and consumes `event-types` (events.py) and `hook-events`
(config.py) owned by hook-capabilities-for-tatara.

## Review focus

- No PTY write while claude responds; runner only after the relay response is sent.
- Turn state invariants across defer -> command -> continue -> complete, interrupt and crash.
- Token handling in claude.json: only `${CCW_*}` references are written; the token value never
  reaches a file.
- Parser fix does not drop real prompts.
- The signal bus never misses a completion signal emitted between typing and waiting (mark before
  typing, wait after the mark).

## Left to the builder

- Exact JSON-RPC error codes other than -32601, the queue cap (4 unless a test shows a reason),
  timeouts within the stated orders of magnitude, the default `continue_prompt` wording.

## Assumptions and unknowns

- R1: settled by the planner's probe (Facts): claude 2.1.304 and 2.1.306 expand `${VAR}` in MCP
  url and headers. No fallback.
- R2: typing too early after Stop; mitigated by the quiet-screen wait and by starting the runner
  only after the Stop relay response is sent.
- R3: PostCompact and compact_boundary when /compact is typed through a PTY assumed from docs and
  local transcripts; the runner accepts either signal and falls back to the stdout failure
  patterns. The fake models both and a boundary-only mode; a real-claude check needs an API call
  and is out of the unit suite.

## Open questions

none

## Irreversible operations in scope

none
