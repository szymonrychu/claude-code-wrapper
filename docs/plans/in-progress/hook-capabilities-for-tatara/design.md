# Hook capabilities for tatara and magic-trainer parity design

## Summary

ccw gains generic hook capabilities (K1-K7), minimal generic core options where hooks cannot do
the job (K8-K13), and three defect fixes (DEF-1..3). Worked examples under
`src/claude_code_wrapper/data/examples/` show how tatara and magic-trainer would configure and
script hooks on top of them, each exercised against the fake claude. No consumer repo changes.

Prefixes: C/ = `src/claude_code_wrapper`, CR = README.md, M/ = magic-trainer
`code/magic-cards-model-api/src/mtg_agent/wrapper`, D/ = `.../mtg_agent/driver`, W/ =
tatara-new `code/tatara-claude-code-wrapper` at origin/main. Row ids (#n, A1..F3) refer to the
parity reports summarised in Research. ccw at f9cb057, installed claude 2.1.306, image pins 2.1.304.

## Intent and why

Both projects run their own Claude Code harness. ccw already matches their interactive core; the
remaining gaps are domain behaviour (finalisers, outcome re-prompts, TTL gates, reviews, metrics
push, background-task-aware completion). The user wants those expressed as hooks so ccw stays
generic and both projects can later move onto it. This epic builds every missing hook capability
and documents the usage with examples; the actual migration is a later decision.

## Goals

- Every non-deferred tatara and magic-trainer gap is implementable with ccw hooks or config.
- magic-trainer review runs as an optional onDestroy hook (one-shot `claude -p` before teardown),
  output returned in the DELETE response and as an event; an option exits the process when the
  last session is destroyed.
- DEF-1..3 fixed.
- Examples for tatara and magic-trainer in this repo, tested against the fake claude.
- README documents every new event, result field, option and env var.

## Non-goals

- Replacing the harness in tatara or magic-trainer; no consumer repo changes.
- OIDC / Keycloak JWT auth; tatara API reshaping (paths, default session per pod, field names,
  contractVersion, compat facade); 403 vs 410 status mapping.
- ros2-lerobot-sverve-platform.
- Relaying TeammateIdle / TaskCompleted as hook events.

## Anti-goals

- No tatara- or mtg-named code paths in ccw core (`src/claude_code_wrapper/*.py`). Domain names
  appear only under `data/examples/`.
- No secrets pushed into claude's env or settings.json to make a hook work.

## Constraints

- TDD per repo rules; tests use the fake claude (`tests/fake_claude/fake_claude.py`), never a real
  claude. Only fake-claude changes may touch `tests/fake_claude/` scenario support; `tests/conftest.py`
  only via the fake-claude owner task.
- `mise run lint` and `mise run test` green; plain ASCII.
- Existing behaviour unchanged when no new option or hook is configured (all defaults off).

## Facts that shape the design (verified, file:line)

- Hook actions get the FULL service env: HookDispatcher base_env=self.service_env
  (C/manager.py:1111-1118), service_env=dict(os.environ) (C/service.py:199), merged at
  C/hooks.py:295-303. README says "child environment" (CR:969-971): the doc is wrong.
- setup_commands get the tool env, not the service env: BASE_ENV + passthrough + claude env + mise
  + CLAUDE_CONFIG_DIR + GIT_CONFIG_GLOBAL (C/manager.py:576-589). They run before
  ensure_config_dir/render_settings (C/manager.py:404-420, 481-507); render_settings deep-merges an
  existing settings.json (C/bootstrap/settings.py:88,119-128).
- onTurnComplete is awaited inside the Stop relay (_on_stop -> _after_turn -> _fire,
  C/router.py:372-395,414-419) and is gated (C/bootstrap/settings.py:18-24): a blocking finaliser
  holds claude in its Stop hook. Domain finalisers must be non-blocking.
- onDestroy fires after claude stopped and the router closed, before purge (C/manager.py:730-757,
  purge 709-712); budget _hook_bound (C/manager.py:1145-1154), capped at 25% of
  shutdown_timeout_seconds on shutdown (C/manager.py:970). DELETE returns _persisted_snapshot only
  (C/manager.py:708) and takes no body (C/api/sessions.py:52-54).
- Hook results honour only decision allow/deny, reason, additional_context (C/hooks.py:79-93);
  deny counts only on VETO_EVENTS (C/config.py:42, C/hooks.py:218).
- Session.complete ends the turn at the first matching Stop (C/session.py:720-768).
- tool_call/tool_result drop agent_id/agent_type (C/router.py:333,355-360); usage transcript events
  carry agent_id (C/transcript.py:54-57,93-99) but are aggregated per model only (C/router.py:477-478).
- Probe token hard-coded ALIVE_TOKEN (C/router.py:55,500); probe stores no answer (C/session.py:917-924).
- TurnRequest is extra=forbid (C/api/turns.py:33-42).
- Upstream (claude 2.1.306; docs https://code.claude.com/docs/en/hooks, local changelog):
  Stop/SubagentStop input has `background_tasks` and `session_crons` (2.1.145), entries
  {id,type,status,description,...}; the lead's Stop lists live in-process teammates (types include
  in_process_teammate, local_agent, local_bash, local_workflow, monitor_mcp). agent_id/agent_type
  are present in Pre/PostToolUse inside subagents and teammates (2.1.69; teammates 2.1.290).
  Teammates do not fire the parent Stop (they fire TeammateIdle). Stop "block"+reason continues the
  turn, stop_hook_active true on re-entry, capped at 8 consecutive blocks
  (CLAUDE_CODE_STOP_HOOK_BLOCK_CAP, 2.1.143).

## Decisions

### D1: Close gaps with generic hook capabilities, core changes only where hooks cannot
User decision. Domain behaviour lives in hook scripts; ccw adds generic events, result fields and
options. Alternatives: port tatara/mtg logic into ccw core; a compat facade. Why: keeps ccw generic,
lets both projects migrate later. Serves A, B, C.

### D2: DEF-1 PreToolUse fails closed, following the matching gate's on_error
handle_claude_hook catches every exception and returns allow (C/router.py:307-316); _fire swallows
dispatcher exceptions into allow (C/router.py:222-224); fire() allows on an unusable event
(C/hooks.py:196-198); redaction runs outside any per-action guard (C/hooks.py:201). Fix: for
PreToolUse compute the fail decision as the strictest on_error among blocking onAgentToolCall
actions whose matcher matches tool_name (shared, matcher-aware version of _gate_fail,
C/manager.py:591-593); on any exception return deny_output("ccw hook relay failed: <kind>"), emit
error and tool_denied; router._fire re-raises for onAgentToolCall. No gate configured: nothing to
fail closed. Alternative: always deny on router error (my call: follow on_error). Serves D.

### D3: DEF-2 busy check before turn files
safe_write_files (C/router.py:439-445) runs before session.submit raises busy
(C/session.py:619-621). Add Session.check_submittable() (writable PTY + busy, sync); call it before
onTurn (no veto hooks for a doomed turn) and again right before safe_write_files; no await between
it, the write and submit's reservation (C/session.py:619-636). Serves D.

### D4: DEF-3 fix the doc, keep hook env as the full service env
Hooks are operator-authored config; finalisers need secrets (callback HMAC, git token, CCW auth)
that must not reach claude's env. README CR:969-971 states: hooks get the full service env, the
action env, CCW_EVENT/SESSION_ID/TURN_ID/WORKDIR/CLAUDE_CONFIG_DIR plus K4 vars; setup_commands get
the allow-listed tool env. Alternative: narrow hook env (forces secrets via passthrough into claude).
(my call, research pick). Serves D.

### D5: K1 stop-gate - new onStop event with allow, block, defer
Fired from _on_stop BEFORE Session.complete, only when the Stop matches the running turn (factor the
session_id/marker checks of C/session.py:734-750 into Session.stop_target()). data: turn_id,
stop_hook_active, last_assistant_message, background_tasks, session_crons, transcript_path.
Results: `{"decision":"block","reason":R}` -> relay returns block+reason to claude, turn stays
running, usage not reset, `turn_continued` event; `{"decision":"defer"}` -> claude answered allow,
turn stays busy, `turn_deferred` event with background_tasks, stall watch and turn_max_seconds still
bound it, interrupt still has a target; allow -> complete as today. Built-in
`sessions.defer_stop_on_background_tasks: bool` (default false) defers when background_tasks is
non-empty, evaluated before onStop hooks; a hook allow cannot override a defer. onStop joins
HOOK_EVENTS and _GATED_EVENTS; decisions allow|block|defer (deny invalid). A later matching Stop with
empty background_tasks completes the deferred turn. Closes mtg #5, #11 caveat, tatara B6. Serves A, B.

### D6: K2 hook annotations
A blocking result may carry `"annotations": {...}`; on turn-scoped events (onTurn, onStop,
onAgentToolCall, onToolResult, onTurnComplete) it is shallow-merged into TurnRecord.annotations
(new field), persisted, shown in GET turn and in callbacks. Closes mtg #5 (pending_tasks). Serves A.

### D7: K3 destroy-io - DELETE body hook_input, hook output returned
DELETE /v1/sessions/{id} accepts an optional JSON body `{"hook_input": {...}}` (capped, default
1 MiB), passed as onDestroy data.input. Any blocking action may return `"output": <json>`, recorded
(redacted, capped) in the hook_result event; for onDestroy it is returned in the DELETE response as
`hook_outputs: [{index, ok, output, error}]` and emitted as a `destroy_output` event before
session_closed. The review runs in onDestroy after claude stopped, before purge and before DELETE
returns (user decision: review as a destroy hook before teardown; after-stop timing is my call, to
avoid two claude processes plus MCP under one pod memory limit). Document that onDestroy timeouts are
bounded by action timeout_seconds on DELETE but 25% of shutdown_timeout_seconds on SIGTERM.
Alternatives: new POST .../destroy route; staging the prompt as a final-turn file. Closes mtg #18.
Serves B.

### D8: K4 hook context env
`CCW_STATE_DIR` = <session_dir>/hook-state (created, kept across resume, removed on purge) and
`CCW_API_URL` = http://127.0.0.1:<server.port> in every hook action env. Supports B4, B6, D1, D6, E1,
mtg #18. Serves A, B.

### D9: K5 agent attribution and per-message usage events
agent_id/agent_type from the hook payload go into tool_call/tool_result events and
onAgentToolCall/onToolResult data; a `usage` event per assistant message (message_id, model,
agent_id, token counts). Closes mtg #13 (ccw part), #15 per-role, #31. Serves A.

### D10: K6 turn metadata
POST turns accepts `metadata: {}` (capped 16 KiB) into onTurn data.metadata and
TurnRecord.metadata. Closes tatara D1 (handoff flag) with a K4-based onTurn TTL gate. Serves B.

### D11: K7 onTick timed hook
New event `onTick`; actions take `interval_seconds` (required) and `when: busy|always` (default
busy), valid only on onTick (validator like matcher, C/config.py:414-425). Per-session timer owned
by the router, cancelled in close(). data: turn_id or null, elapsed_seconds. Closes tatara D6
(safety push), E1 (metrics push). Serves B.

### D12: K8 exit policy
`sessions.exit_when_last_destroyed: bool`: after a DELETE or the idle reaper empties the active set
and at least one session was created, the service stops and exits 0.
`sessions.exit_when_dead: bool`: a session reaching dead stops the service with exit 3 and writes
/dev/termination-log when writable. Not hooks: `kill -TERM $PPID` gives the wrong exit code, no
termination message and races the dispatcher drain (C/manager.py:1001-1004). Files: config.py,
manager.py, service.py (stop event + code, C/service.py:220-226), cli.py. Closes mtg #9 (user
decision: exit on last destroy), tatara D5. Serves B.

### D13: K9 configurable probe token and captured answer
`sessions.probe_alive_token` (default CCW-ALIVE) replaces ALIVE_TOKEN (C/router.py:55,500); the
probe record gains `answer` (matching text, capped). Core: probe state is core
(C/session.py:909-924). Closes tatara A9. Serves C.

### D14: K10 outstanding subagents in the snapshot
The router keeps the set of agent_ids between SubagentStart and SubagentStop (C/router.py:397-407)
and the session snapshot exposes `outstanding_subagents` (count). Closes tatara A6 count. Serves C.

### D15: K11 passthrough env globs
`claude.passthrough_env` entries become fnmatch patterns plus `claude.passthrough_env_exclude`;
CCW_* and managed names stay refused (C/session.py:191-195); applied in build_env and _tool_env
(C/manager.py:581-583). Closes tatara C2 (arbitrary ExtraEnvs), feeds bootstrap helper env.
Serves C.

### D16: K12 turn continuity across a mid-turn crash
`sessions.restart_continue_prompt` (empty = off). When claude exits mid-turn with restarts left, the
record stays running instead of failing with claude_exited (C/session.py:525-527); after the resumed
relaunch (C/session.py:562-567) the continue prompt is pasted with the same turn marker; fail as
today when the budget is exhausted. Alternative: accept failed + operator resubmit (my call: build
it). Closes tatara D4. Serves C.

### D17: K13 missing metric families
ccw_http_requests_total{route,method,status_code}, ccw_http_panics_total (middleware),
ccw_probe_outcomes_total{outcome}, ccw_probe_answer_seconds, ccw_stall_suspected_total,
ccw_interrupts_total{result}. Label renames for tatara dashboards are consumer work. Closes tatara
E2. Serves C.

### D18: Examples for tatara and magic-trainer live in this repo
User decision ("add examples with how to use both in the code"). Under
`src/claude_code_wrapper/data/examples/`: a tatara example (config.yaml plus hook scripts for
bootstrap via setup_commands, non-blocking signed finaliser, issue capture, outcome re-prompt gate on
onStop, onTurn TTL gate on metadata, statusline, onTick safety push and metrics push, exit_when_dead,
probe token) and a magic-trainer example (config.yaml plus scripts for defer_stop_on_background_tasks
with the onStop fallback pending-tasks script, onDestroy review with a claude -p skeleton reading
data.input and printing output, exit_when_last_destroyed, activity narration, gitconfig and agents
via setup_commands). Scripts are self-contained (python stdlib or sh), carry no secrets, and each
example has a test that loads its config with `ccw check-config` semantics and drives the key hooks
against the fake claude. README links them. The research recommended consumer-repo homes for the
real kits; that is for the later migration. Serves E.

## Research

| Lens | Ran / skipped | Adopted | Rejected |
|---|---|---|---|
| research-code-internal | ran (three opus parity agents + architect-researcher over ccw, tatara harness, mtg harness) | K1-K13, DEF-1..3, facts above (file:line) | porting domain logic into core |
| research-code-external | skipped: in-house glue on top of Claude Code hooks; the only external surface is the hooks API, covered by research-docs | | |
| research-papers | skipped: no algorithmic core | | |
| research-docs | ran inside architect-researcher: Claude Code hooks docs https://code.claude.com/docs/en/hooks and local changelog for 2.1.69, 2.1.143, 2.1.145, 2.1.163, 2.1.290, binary strings of 2.1.306 | background_tasks in Stop input, agent_id in tool payloads, Stop block cap 8 | relying on teammate Stops (they fire TeammateIdle) |

Parity reports (scratchpad, summarised here): magic-trainer verdict "replace with adapter work",
blockers seat-pair completion (#5), review route (#18), process-exit lifecycle (#9), busy-before-files
(#2b), agent_id on tool events (#13). tatara verdict "adapter work, large": API shape (deferred),
turn-complete callback (B1-B3, hooks), bootstrap domain logic (C3-C8, setup_commands), pod TTL (D1),
probe marker (A9), auth (deferred), metrics (E1-E2).

Row classification (A = today's hooks/config, B = hook after Kn, C = core Kn):
- mtg: #2 as is; #2b DEF-2; #3 config; #5 B K1+K2; #9 C K8; #10 config preStop; #11 as is (caveat
  K1); #13 B K5; #14 A (jq narration hooks); #15 B K5; #17 config max_restarts 0; #18 B K3+K4;
  #19 A setup_commands (+K11 globs); #20-#25 config; #26-#30 as is; #31 B K5; #1,#4,#7,#8,#12,#16,
  #32-#34 consumer.
- tatara: A6 count C K10; A7, A11 as is; A9 C K9; B1-B3 A non-blocking finaliser; B4 A
  onAgentToolCall matcher capture (+K4); B5 A statusLine setting; B6 B K1 outcome gate; C1 A; C2 A /
  C K11; C3-C8 A setup_commands helper; C9-C11 A; D1 B K6+K4; D2 as is (needs K9,K10); D4 C K12;
  D5 C K8; D6 B K7; E1 B K7; E2 C K13; deferred A1-A5, A6 renames, A8, A10, B7 mapping, D3; E3,
  F1-F3, C12 consumer.

## Research inputs

| Source | Entry | Lands in |
|---|---|---|
| architect-researcher section 3 | DEF-1 PreToolUse relay fails open | pretooluse-gate-fails-closed |
| architect-researcher section 3 | DEF-2 turn files written before the busy check | busy-check-before-turn-files |
| architect-researcher section 3 | DEF-3 README says hooks get the child env | hook-context-env |
| architect-researcher section 4 | K1 stop gate (onStop allow/block/defer, defer flag) | stop-gate-onstop-event, hook-result-fields, fake-claude-hook-scenarios |
| architect-researcher section 4 | K2 hook annotations | turn-annotations-from-hooks, hook-result-fields |
| architect-researcher section 4 | K3 destroy hook input and output | destroy-hook-input-output, hook-result-fields |
| architect-researcher section 4 | K4 CCW_STATE_DIR and CCW_API_URL | hook-context-env |
| architect-researcher section 4 | K5 agent attribution and usage events | agent-attribution-and-usage, fake-claude-hook-scenarios |
| architect-researcher section 4 | K6 turn metadata | turn-metadata-on-submit |
| architect-researcher section 4 | K7 onTick | ontick-timed-hook |
| architect-researcher section 4 | K8 exit policy | service-exit-policy |
| architect-researcher section 4 | K9 probe token and answer | probe-token-and-answer |
| architect-researcher section 4 | K10 outstanding subagents | outstanding-subagent-count |
| architect-researcher section 4 | K11 passthrough globs | passthrough-env-globs |
| architect-researcher section 4 | K12 turn continuity | turn-continuity-across-crash, fake-claude-hook-scenarios |
| architect-researcher section 4 | K13 metric families | missing-metric-families |
| user | examples for tatara and mtg in this repo | tatara-example-turn-hooks, tatara-example-lifecycle, magic-trainer-example |
| parity-magic #18 | review via claude -p (fake needs print mode) | fake-claude-hook-scenarios, magic-trainer-example |
| Assumptions R1 | teammates may drop out of background_tasks | magic-trainer-example (pending-tasks fallback) |
| Assumptions R2 | claude caps consecutive Stop blocks at 8 | stop-gate-onstop-event (README), tatara-example-turn-hooks (count in CCW_STATE_DIR) |
| Assumptions R3 | SIGTERM cuts onDestroy to 25% of the shutdown budget | destroy-hook-input-output (README) |
| Assumptions R4 | blocking onTurnComplete holds claude's Stop hook | tatara-example-turn-hooks (non-blocking finaliser) |
| Assumptions R5 | passthrough "*" exposes service secrets | passthrough-env-globs (README warning), tatara-example-lifecycle (globs plus excludes) |
| parity-tatara A1-A6 renames, A8, A10, B7, D3 | API reshaping and OIDC | rejected: deferred by user decision |
| parity-ros2 | ros2 parity | rejected: deferred by user decision |
| parity-tatara E1 alternative | built-in push client | rejected: onTick metrics-push hook covers it (D11) |

## Decision coverage

| Decision | Tasks |
|---|---|
| D1 Close gaps with generic hook capabilities | hook-result-fields, stop-gate-onstop-event, ontick-timed-hook, tatara-example-turn-hooks, tatara-example-lifecycle, magic-trainer-example |
| D2 DEF-1 PreToolUse fails closed | pretooluse-gate-fails-closed |
| D3 DEF-2 busy check before turn files | busy-check-before-turn-files |
| D4 DEF-3 hook env doc | hook-context-env |
| D5 K1 stop-gate | fake-claude-hook-scenarios, hook-result-fields, stop-gate-onstop-event |
| D6 K2 hook annotations | hook-result-fields, turn-annotations-from-hooks |
| D7 K3 destroy-io | hook-result-fields, destroy-hook-input-output |
| D8 K4 hook context env | hook-context-env |
| D9 K5 agent attribution and usage events | fake-claude-hook-scenarios, agent-attribution-and-usage |
| D10 K6 turn metadata | turn-metadata-on-submit |
| D11 K7 onTick | ontick-timed-hook |
| D12 K8 exit policy | service-exit-policy |
| D13 K9 probe token and answer | probe-token-and-answer |
| D14 K10 outstanding subagents | outstanding-subagent-count |
| D15 K11 passthrough env globs | passthrough-env-globs |
| D16 K12 turn continuity | fake-claude-hook-scenarios, turn-continuity-across-crash |
| D17 K13 metric families | missing-metric-families |
| D18 Examples in this repo | tatara-example-turn-hooks, tatara-example-lifecycle, magic-trainer-example |

## Contracts

| Part | Owner | Consumers | Where defined |
|---|---|---|---|
| `fake-claude-scenarios` | fake-claude-hook-scenarios | stop-gate-onstop-event, agent-attribution-and-usage, turn-continuity-across-crash, tatara-example-turn-hooks, magic-trainer-example | `tests/fake_claude/fake_claude.py` |
| `ccw-world-fixture` | fake-claude-hook-scenarios | hook-context-env, stop-gate-onstop-event, turn-annotations-from-hooks, agent-attribution-and-usage, turn-metadata-on-submit, ontick-timed-hook, probe-token-and-answer, outstanding-subagent-count, turn-continuity-across-crash, destroy-hook-input-output, service-exit-policy, tatara-example-turn-hooks, tatara-example-lifecycle, magic-trainer-example | `tests/conftest.py` |
| `hook-result-fields` | hook-result-fields | stop-gate-onstop-event, turn-annotations-from-hooks, destroy-hook-input-output, tatara-example-turn-hooks, magic-trainer-example | `src/claude_code_wrapper/hooks.py` |
| `hook-context-env` | hook-context-env | tatara-example-turn-hooks, tatara-example-lifecycle, magic-trainer-example | `src/claude_code_wrapper/hooks.py` |
| `on-stop-event` | stop-gate-onstop-event | turn-annotations-from-hooks, tatara-example-turn-hooks, magic-trainer-example | `src/claude_code_wrapper/config.py` |
| `turn-annotations` | turn-annotations-from-hooks | magic-trainer-example | `src/claude_code_wrapper/turns.py` |
| `agent-attribution` | agent-attribution-and-usage | magic-trainer-example | `src/claude_code_wrapper/router.py` |
| `turn-metadata` | turn-metadata-on-submit | tatara-example-lifecycle | `src/claude_code_wrapper/turns.py` |
| `on-tick-event` | ontick-timed-hook | tatara-example-lifecycle | `src/claude_code_wrapper/config.py` |
| `probe-alive-token` | probe-token-and-answer | missing-metric-families, tatara-example-lifecycle | `src/claude_code_wrapper/config.py` |
| `passthrough-globs` | passthrough-env-globs | tatara-example-lifecycle | `src/claude_code_wrapper/session.py` |
| `destroy-io` | destroy-hook-input-output | magic-trainer-example | `src/claude_code_wrapper/manager.py` |
| `exit-policy` | service-exit-policy | tatara-example-lifecycle, magic-trainer-example | `src/claude_code_wrapper/config.py` |

Cross-epic registry (`docs/plans/contracts.json`, owner this epic): `hook-result-protocol`
(hooks.py), `hook-events` (config.py HOOK_EVENTS), `event-types` (events.py EVENT_TYPES),
`hook-context-env-vars` (hooks.py). They extend parts the done epic claude-code-wrapper-microser
built (hook-dispatcher, config-schema, event-log); no live epic consumes them yet.

## Review focus

- DEF-1: every exception path in the PreToolUse relay must deny when a matching blocking gate has
  on_error deny; tests force each path.
- K1 defer/block: turn state machine invariants (no double completion, interrupt and
  turn_max_seconds still reach a deferred turn, usage not reset on block).
- K12: no lost or duplicated turn on crash; restart budget respected.
- K8: exit codes and that shutdown still drains hooks.

## Left to the builder

- Exact caps (hook_input 1 MiB, metadata 16 KiB, output and answer caps) within those orders of
  magnitude; event field names beyond the ones named here.
- Example script language (python stdlib or sh) per script.
- builder check 2026-10-21: magic-trainer-example depends on tatara-example-lifecycle (both edit README `## Hook examples`).
- builder check 2026-10-21: tatara-example-lifecycle AC6 is `doc-section:README.md#tatara lifecycle`; the requirement creates the `#### tatara lifecycle` heading (magic-trainer and turn-hooks doc-section headings are likewise created by their requirements).
- builder check 2026-10-21: onStop annotations merge into the turn record on every leg (allow, block, defer); turn-annotations-from-hooks owns the merge call, with a defer/block-leg AC.
- builder check 2026-10-21: service-exit-policy: with exit_when_last_destroyed the DELETE returns 200 with its full body (hook_outputs included) before exit 0; the stop is scheduled after the response is sent.
- builder check 2026-10-21: hook_outputs collects only blocking onDestroy actions (README AC); magic-trainer's review action sets blocking: true.
- builder check 2026-10-21: hook-context-env: CCW_CLAUDE_CONFIG_DIR (hook env) and CLAUDE_CONFIG_DIR (claude) of the same session resolve to the same path (test AC).
- builder check 2026-10-21: EVENT_DECISIONS in hook-result-fields covers existing events only; stop-gate-onstop-event adds onStop (allow|block|defer), ontick-timed-hook adds onTick (allow only).

Settled by the planner (my call), binding on the tasks:

- onStop: a command exit 2 is a block (claude's own Stop convention); across actions block > defer > allow.
- onTurnComplete annotations are persisted and served by GET turn; the callback already sent is not re-sent.
- K12 emits a `turn_resumed` event; a fresh (non-resumed) relaunch cannot continue and fails the turn.
- K8 writes the termination message to the module constant `TERMINATION_LOG` (`/dev/termination-log`), skipped when not writable.
- K13 http route label is the matched route template, `unmatched` otherwise.
- hook `output` cap 256 KiB, annotations 16 KiB per turn, probe answer 2000 chars, DELETE body 1 MiB, metadata 16 KiB.
- Examples live at `data/examples/<name>.config.yaml` plus `data/examples/<name>/` scripts (python stdlib), symlinked into `examples/` like workbench.

## Assumptions and unknowns

- R1: an in-process teammate idle between its own turns may drop out of background_tasks. Not
  probed (needs a real session with CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1). Mitigation shipped in
  the mtg example: an onStop pending-tasks fallback script reading the transcript.
- R2: claude caps consecutive Stop blocks at 8; gate scripts count via CCW_STATE_DIR.
- R3: on SIGTERM onDestroy is cut to 25% of the shutdown budget; reviews need DELETE.
- R4: blocking onTurnComplete holds claude's Stop hook; README and examples use non-blocking
  finalisers.
- R5: passthrough "*" would expose service secrets to claude; examples use explicit globs plus
  excludes.

## Open questions

none

## Irreversible operations in scope

none
- builder ruling 2026-10-21: dropped discovered task mise-shim-for-ruff-has-no-ve: `mise run lint` uses `uv run ruff` and passes in every worktree; the reported shim error came from the verifier environment running bare ruff, not from the repo.
