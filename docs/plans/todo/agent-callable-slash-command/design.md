# Agent-callable slash commands and idle auto-compact design

## Summary

ccw serves an MCP server (`ccw`) over HTTP on the per-session internal API. Its tools queue
allowlisted Claude Code slash commands (`/compact [instructions]`, `/reload-plugins`, and more via
`run_command`). At the turn's Stop ccw holds the turn (defer leg), types the command at the idle
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

- Main agent can self-compact and self-reload plugins through MCP tools; other allowlisted
  commands through `run_command`.
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

## Decisions

### D1: Agent interface is a ccw MCP server over HTTP on the internal API
`/internal/mcp`, JSON-RPC (initialize, tools/list, tools/call, ping; JSON responses; GET 405),
authenticated with the existing per-session internal token. Injected into the session's claude.json
`mcpServers.ccw` with `type: http`, url and `Authorization` header as `${CCW_*}` env references so
the token is not stored; fallback (if header expansion fails on the pinned claude, probed in the
integration test): literal token in the 0600 claude.json, regenerated every activation. `alwaysLoad:
true`, server `instructions` explain the tool. ONE tool (user decision): `command` with input
schema `{name: enum of the session's agent allowlist, params?: object}`; params are optional and
per command (compact: `instructions?: string`; mcp: `action: "reconnect"`, `server?: string`
default all; reload-plugins: `force?: bool`, honoured only when allow_force; reload-skills and
context: none). The schema documents each command's params (oneOf per name or a described object);
unknown params are rejected with a tool error naming the allowed ones. settings.json allow gets
`mcp__ccw__command`; operators restrict commands through the allowlist, not per tool. The public
endpoint (D5) is deliberately redundant with the turns endpoint for clients (user accepted).
Alternatives: CLI via Bash (dead when Bash is denied, opaque to gates), PreToolUse pseudo tool
(rejected). (research pick, my call on fallback)

### D2: Main agent only
A call whose hook context carries an agent_id (subagent or teammate; known from the PreToolUse relay
for `mcp__ccw__*`, which ccw sees) is refused with a clear tool error. User decision.

### D3: Command queue and allowlist
`router.request_command(name, args, origin=agent|client|idle)`. Built-in set {compact,
reload-plugins, reload-skills, mcp (reconnect only), context}; everything else is rejected at config
validation. `sessions.commands: {agent: [compact, reload-plugins], client: [compact, reload-plugins,
reload-skills, mcp, context], timeout_seconds: {compact: 600, default: 60}, continue_prompt: <text>,
allow_force: false}`, overridable per session on CreateSessionRequest. Coalescing: a duplicate name
already queued returns the existing id (compact keeps the first instructions). Queue cap 4. Each
command gets a record {id, name, args, origin, state queued|running|completed|failed|cancelled,
result, error, timestamps}. `--force` only when allow_force. The agent tool returns success:
"Queued <id>. It runs when you end this turn; end your turn now with a one-line status. You will be
resumed automatically."

### D4: Same-turn lifecycle (user decision)
1. Request: validate, queue, `command_queued` event.
2. Stop: `_stop_gate` also gates when commands are pending. onStop block wins (command stays
   queued). Background tasks pending: defer as today, stays queued. Else answer allow, emit
   `turn_deferred{reason: "command"}`, start the runner after the relay response is sent.
3. Runner: wait for a quiet screen (>= boot.idle_ready_seconds); fire `onCommand` (blocking, can
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

### D5: Public endpoint (user decision)
`POST /v1/sessions/{id}/commands {name, args?}` -> 202 with the record (client allowlist);
`GET /v1/sessions/{id}/commands/{command_id}` and list. Same queue.

### D6: Idle auto-compact
`sessions.auto_compact: {idle_seconds: 0 (off), instructions: "", min_context_tokens: 0}`. Idle =
state ready, no turn, no command, no event for idle_seconds. Armed when a turn ends, disarmed after
firing (once per idle gap). Skipped below min_context_tokens (last main-chain usage input + cache).
Reaper clock ignores events with origin idle; validator error when idle_seconds >=
idle_timeout_seconds > 0. Exit policies unaffected; shutdown abandons a running command.

### D7: Events, hooks, metrics
Events: command_queued, command_started, command_completed, command_failed, compacted{trigger,
pre_tokens, post_tokens, duration_ms} (for every compaction, including claude's own auto/idle),
plus turn_resumed cause. Hooks: onCommand (veto), onCommandComplete. Relay PreCompact and PostCompact
(new relay events). Metrics: ccw_commands_total{command,origin,result},
ccw_command_duration_seconds{command}, ccw_compactions_total{trigger}. Update
tests/test_event_log.py pin and README `### <event>` sections.

### D8: Transcript parser fix (always on)
parse_line skips user entries with isCompactSummary, isMeta, `<command-name>` or
`<local-command-*>` content as prompts; adds CompactBoundary and CommandOutput events for system
lines. Prevents stale markers from compact summaries making Stops look stale.

### D9: Examples and docs
README section "Slash commands" (tool, endpoint, lifecycle, config, auto-compact, limits); example
config snippet in data/examples; tatara and magic-trainer examples get an auto_compact block.

## Research

| Lens | Ran / skipped | Adopted | Rejected |
|---|---|---|---|
| research-code-internal | ran (architect-researcher over C/ at 4b1d358) | defer + continue reuse, internal API auth, seed_claude_json mcpServers | new process for MCP |
| research-docs | ran (code.claude.com docs commands, hooks, mcp, settings-reference; changelog; binary strings 2.1.306) | completion signals, header expansion, allowlist | /clear, auto --force |
| research-code-external | skipped: no external analogue needed beyond MCP spec covered by docs | | |
| research-papers | skipped: no algorithmic core | | |

## Research inputs

| Source | Entry | Lands in |
|---|---|---|
| architect-researcher | D1-D9 | planner fills task slugs |

## Decision coverage

| Decision | Tasks |
|---|---|
| D1..D9 | planner fills task slugs |

## Contracts

Planner fills: command record model, router.request_command, internal MCP JSON-RPC surface, new
event types, fake-claude slash-command support.

## Review focus

- No PTY write while claude responds; runner only after the relay response is sent.
- Turn state invariants across defer -> command -> continue -> complete, interrupt and crash.
- Token handling in claude.json (env expansion; fallback file mode 0600).
- Parser fix does not drop real prompts.

## Left to the builder

- Exact JSON-RPC error codes, queue cap, timeouts within the stated orders of magnitude.

## Assumptions and unknowns

- R1: header `${VAR}` expansion on 2.1.304 unverified; integration test against the real container
  path (fake claude cannot prove it) and the literal-token fallback.
- R2: typing too early after Stop; mitigated by quiet-screen wait.
- R3: PostCompact and compact_boundary when /compact is typed through a PTY assumed from docs and
  local transcripts; fallback to stdout pattern matching.

## Open questions

none

## Irreversible operations in scope

none
