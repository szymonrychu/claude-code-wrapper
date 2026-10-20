---
title: Event router
status: in-progress
repo: claude-code-wrapper
value: Every hook call and transcript line becomes SSE events and user hook calls
files: [src/claude_code_wrapper/router.py, tests/test_router.py]
verify: "uv run pytest tests/test_router.py -q && mise run lint"
depends_on: [session-turns, event-log, hook-dispatcher, transcript-tailer, workdir-init, hook-relay]
owns: [event-router]
uses: [session, turn-store, event-log, hook-dispatcher, transcript-tailer, workdir-init, hook-relay-protocol, redactor]
model: ""
estimate: 90
actual: ""
review: full
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/claude-code-wrapper-microser--event-router
branch: claude-code-wrapper-microser--event-router
verified_head: ""
run: claude-code-wrapper-microser
provides: []
acs_for_audit: []
process_gaps: []
---

# Event router

## Requirement

Build the per-session event router (D6, D7, D11) in `src/claude_code_wrapper/router.py`, wiring a Session, its EventLog, the HookDispatcher, the transcript tailers and the redactor.

`SessionRouter(session, event_log, dispatcher, redactor, cfg, *, session_info)` implements `SessionListener` and exposes:
- `async handle_claude_hook(event_name, payload) -> RelayResponse` (called by the internal API). Every branch first calls `session.note_activity()`; a payload `transcript_path` different from the tailer's path triggers `tailer.switch(path)`.
  - UserPromptSubmit: path tracking only; returns `{stdout: "", stderr: "", exit_code: 0}`.
  - PreToolUse: when `tool_name == "AskUserQuestion"` and `sessions.ask_user_question == "forward"`: emit `question` `{tool_use_id, questions: tool_input}`, return deny output with reason `Question forwarded to the API client. Ask the user in plain text, end your turn now and wait for their reply in the next message.` Otherwise emit `tool_call` `{tool_use_id, tool_name, tool_input}` (redacted), fire `onAgentToolCall` with `tool_name`; deny -> emit `tool_denied` `{tool_use_id, tool_name, reason}` and return `deny_output(reason)`; allow with additional_context -> stdout `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": ...}}`; else empty stdout.
  - PostToolUse / PostToolUseFailure: emit `tool_result` `{tool_use_id, tool_name, is_error, response}` (response text truncated to 4000 chars, redacted), increment the turn's `tool_calls`, fire `onToolResult`; additional_context returned as PostToolUse `hookSpecificOutput.additionalContext`.
  - Stop: `session.complete(payload, usage=..., model_tokens=..., marker_turn_id=<last marker seen in the transcript>)`; a returned record -> emit `turn_completed` (record, redacted), fire `onTurnComplete`, and when the record has `callback_url` deliver it with `post_json` signed with `server.callback_secret`; None -> nothing.
  - SubagentStart / SubagentStop: emit `subagent_started` / `subagent_stopped`, fire `onSubagentStart` / `onSubagentStop`.
  - Any exception: logged, `error` event `{kind: "hook_relay", message}`, `onError` fired, and an allow response (exit 0, empty stdout) returned. Claude is never broken by the router.
- Transcript events (from TranscriptTailer and SubagentWatcher): `AssistantText` -> `assistant_text` `{text, message_id, model, agent_id}`; main-thread text also fires `onAgentResponse`; `Thinking` -> `thinking`; `Usage` -> UsageTotals for the current turn (reset at submit); `Interrupted` -> `session.on_interrupted()`; `UserPrompt.turn_marker` -> remembered as the last marker; `ProbeMarker` -> `session.probe_delivered`; assistant text containing `CCW-ALIVE` after a delivered probe -> `session.probe_answered()` and a `probe` event. Every batch calls `note_activity()`.
- `async submit_turn(text, *, files=None, callback_url=None, origin="client") -> TurnRecord`: fire `onTurn` `{text, files: [names], origin}`; deny -> emit `turn_rejected` `{reason}` and raise `TurnVetoed(reason)`; write files with `safe_write_files`; `session.submit(...)`; reset usage totals; emit `turn_started` `{turn_id, origin, text}` (redacted); ensure the tailer and subagent watcher run on the session transcript path.
- `async interrupt()`: `session.interrupt()`; when a turn was interrupted emit `interrupted` `{turn_id}` and fire `onInterrupt`.
- Listener callbacks: `on_state` -> `session_state`; `on_fatal` -> `error` `{kind: "fatal", message}` + `onError`; `on_restart` -> `session_state` with restart info + `onError` `{kind: "restart"}`; `on_exit` -> `error` `{kind: "exited"}` + `onError`; `on_stall` -> `stall_suspected`; `on_turn_end` for failed/interrupted/timeout -> `turn_failed` `{record}`.
- Dispatcher `on_result` -> `hook_result` events.
- `async close()`: stops tailers; emits `session_closed`.

Settled by the builder check (binding):
- `SessionRouter` exposes read-only attributes `session`, `event_log` and `turns` (the TurnStore); it calls `listener.bind` via the Session it is attached to (it subclasses the `SessionListener` base).
- Stop: `await tailer.flush()` BEFORE reading the last turn marker and usage totals, so the marker of the finishing turn is never missed.
- PostToolUse/PostToolUseFailure use `session.record_tool_call()`.
- The `metrics=None` constructor kwarg is added by the metrics task.

Settled at the gate (D19-D24, binding):
- Forwarded AskUserQuestion: besides the `question` event, the event envelope is POSTed with `post_json` (signed with `server.callback_secret`) to the running turn's `callbackUrl`, else the session's `callbackUrl`; none set -> no POST.
- onTurnComplete fires for EVERY terminal turn state (completed, failed, interrupted, timeout) with `state` in its data (the turn record), and the turn's callback (turn `callbackUrl`, else session `callbackUrl`) is delivered for every terminal state too; `turn_failed` is still emitted for the non-completed states.

## Acceptance conditions

- AC1: An allowed PreToolUse emits tool_call and returns empty stdout [evidence: test:tests/test_router.py::test_pretooluse_allow_emits_tool_call]
- AC2: A denied PreToolUse returns deny JSON and emits tool_denied [evidence: test:tests/test_router.py::test_pretooluse_deny_returns_deny_json_and_tool_denied]
- AC3: AskUserQuestion is forwarded as a question event and denied with guidance [evidence: test:tests/test_router.py::test_ask_user_question_forwarded]
- AC4: Hook additional_context is returned to Claude [evidence: test:tests/test_router.py::test_additional_context_returned]
- AC5: PostToolUse emits a redacted, truncated tool_result and fires onToolResult [evidence: test:tests/test_router.py::test_posttooluse_emits_tool_result_redacted]
- AC6: Stop completes the turn, emits turn_completed and fires onTurnComplete [evidence: test:tests/test_router.py::test_stop_completes_turn_and_fires_on_turn_complete]
- AC7: A turn callback_url receives the signed record [evidence: test:tests/test_router.py::test_callback_url_receives_record]
- AC8: Main-thread assistant text fires onAgentResponse [evidence: test:tests/test_router.py::test_assistant_text_fires_on_agent_response]
- AC9: Subagent start and stop emit events and fire hooks [evidence: test:tests/test_router.py::test_subagent_events]
- AC10: Interrupt emits interrupted and fires onInterrupt [evidence: test:tests/test_router.py::test_interrupt_fires_on_interrupt]
- AC11: An onTurn veto rejects the submit without writing to the PTY [evidence: test:tests/test_router.py::test_on_turn_veto_rejects_submit]
- AC12: An exception in a handler returns allow and emits error [evidence: test:tests/test_router.py::test_exception_in_handler_returns_allow]
- AC13: A fatal screen pattern fires onError [evidence: test:tests/test_router.py::test_fatal_fires_on_error]
- AC14: A changed transcript_path in a hook payload switches the tailer [evidence: test:tests/test_router.py::test_transcript_path_switch]
- AC15: Hook action outcomes are emitted as hook_result events [evidence: test:tests/test_router.py::test_hook_results_emitted]

- AC16: Stop flushes the tailer before reading the turn marker [evidence: test:tests/test_router.py::test_stop_flushes_tailer_before_marker]
- AC17: The router exposes session, event_log and turns [evidence: test:tests/test_router.py::test_router_exposes_parts]

- AC18: A forwarded question is POSTed to the turn or session callbackUrl [evidence: test:tests/test_router.py::test_question_posted_to_callback_url]
- AC19: onTurnComplete fires with state for failed, interrupted and timeout turns [evidence: test:tests/test_router.py::test_on_turn_complete_fires_for_every_terminal_state]

## Files

- `src/claude_code_wrapper/router.py`
- `tests/test_router.py`

## Interfaces

- Part `event-router`: `claude_code_wrapper.router.SessionRouter`, `TurnVetoed`. Consumers: session-manager (one router per session), internal-api (via the manager), metrics (instrumentation).

## Expected outcome

Every Claude hook call and transcript line becomes the right SSE event and user hook invocation, the tool gate answers within the hook timeout, and nothing the router does can wedge claude.

## Steps

- Write `tests/test_router.py` with a real EventLog in tmp, a recording dispatcher double (scripted decisions) and a session double; red.
- Implement branch by branch; green; `mise run lint`.

## Notes

- PreToolUse output format per Claude Code hooks: `hookSpecificOutput.permissionDecision` deny with `permissionDecisionReason`; PostToolUse `hookSpecificOutput.additionalContext` (ros2 `R/runner.py:514-526`).
- The plugin's bash-output-filter rewrites Bash commands via `updatedInput`; the router reports `tool_input` as Claude sent it to PreToolUse (documented in README by readme-api).
- `deny_output` and `RelayResponse` come from `hook_relay.py`; do not redefine them.

- 2026-10-20 run claude-code-wrapper-microser: dependency-failed on session-turns, event-log, hook-dispatcher, transcript-tailer, workdir-init
