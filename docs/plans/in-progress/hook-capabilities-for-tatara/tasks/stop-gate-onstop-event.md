---
title: Stop gate onStop event
status: in-progress
repo: .
value: "Turns can be continued or held open past a Stop, closing mtg seat-pair completion and tatara outcome re-prompts (K1)"
files: [src/claude_code_wrapper/config.py, src/claude_code_wrapper/bootstrap/settings.py, src/claude_code_wrapper/hooks.py, src/claude_code_wrapper/session.py, src/claude_code_wrapper/router.py, src/claude_code_wrapper/events.py, README.md, tests/test_stop_gate.py]
verify: "uv run pytest tests/test_stop_gate.py tests/test_router.py tests/test_session_turns.py tests/test_bootstrap_settings.py tests/test_config.py tests/test_readme_api.py tests/test_readme_config.py -q && mise run lint"
depends_on: [busy-check-before-turn-files, hook-context-env, fake-claude-hook-scenarios]
owns: [on-stop-event]
uses: [hook-result-fields, fake-claude-scenarios, ccw-world-fixture]
model: opus
estimate: 90
actual: ""
review: full
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/hook-capabilities-for-tatara--stop-gate-onstop-event
branch: hook-capabilities-for-tatara--stop-gate-onstop-event
verified_head: ""
run: ""
provides: []
---

# Stop gate onStop event

## Requirement

K1 stop gate (design D5): a new `onStop` hook event that can allow, block or defer the end of the running turn, plus a built-in background-task defer. Owns part `on-stop-event`.

Config and relay:
- `onStop` joins `HOOK_EVENTS` and `HooksConfig` (no matcher). `sessions.defer_stop_on_background_tasks: bool = False`.
- `onStop` joins `_GATED_EVENTS` in `bootstrap/settings.py`, so blocking onStop actions extend the relay timeout.
- `EVENT_DECISIONS` (from hook-result-fields, existing events only) gains `onStop`: this task adds `EVENT_DECISIONS["onStop"]` = `allow|block|defer` (deny on onStop is not honoured: treated as allow and recorded invalid, per the hook-result-fields rule). A command action exiting 2 on onStop is a block with its trimmed stderr as reason. Across blocking actions the outcome is the strictest: block > defer > allow; the reason is the first blocking action's reason in index order.

Session:
- `Session.stop_target(payload, marker_turn_id) -> TurnRecord | None` factors the claude-session-id and stale-marker checks out of `Session.complete`; `complete` uses it. Behaviour of `complete` is unchanged.

Router `_on_stop`, after flushing transcripts:
- onStop fires only when the Stop targets the running turn (a busy session, matching claude session id, not a stale marker). A Stop with no running turn (an agent-origin background completion) and a stale or foreign Stop fire no onStop and behave as today.
- data: `turn_id`, `stop_hook_active`, `last_assistant_message`, `background_tasks`, `session_crons`, `transcript_path` (missing payload keys become `[]`/null).
- Built-in defer: when `defer_stop_on_background_tasks` is true and `background_tasks` is non-empty the decision is at least `defer`, evaluated before the hooks; a hook `allow` cannot override it, a hook `block` can (block wins).
- `block`: the relay returns stdout `{"decision": "block", "reason": R}` to claude; the turn stays running and busy; usage totals are NOT reset; a `turn_continued` event `{turn_id, reason, stop_hook_active}`; no onTurnComplete, no callback.
- `defer`: the relay returns allow; the turn stays running and busy; usage totals are NOT reset; a `turn_deferred` event `{turn_id, background_tasks}`; the stall watch and `limits.turn_max_seconds` still bound it and `interrupt` still ends it.
- `allow`: complete as today (one `turn_completed`, one onTurnComplete, one callback), with usage summed over every leg of the turn.
- A later matching Stop whose decision is allow (e.g. empty `background_tasks`) completes the deferred turn.
- `turn_continued` and `turn_deferred` join `EVENT_TYPES`.

README: `### \`onStop\`` under `## Hooks` (trigger, data, decisions, exit 2, claude's cap of 8 consecutive blocks so gate scripts must count in CCW_STATE_DIR, the defer flag), `### \`turn_continued\`` and `### \`turn_deferred\`` under `## Events (SSE)`, and the `sessions.defer_stop_on_background_tasks` config row. Nothing changes when no onStop action is configured and the flag is off.

## Acceptance conditions

- AC1: an onStop block returns the reason to claude, keeps the turn running and emits turn_continued; the turn completes on the next allowed Stop [evidence: test:tests/test_stop_gate.py::test_block_continues_turn_and_returns_reason]
- AC2: usage of every leg of a blocked turn is summed in the completed record [evidence: test:tests/test_stop_gate.py::test_block_keeps_usage]
- AC3: with defer_stop_on_background_tasks the turn stays busy until a Stop with no background tasks [evidence: test:tests/test_stop_gate.py::test_defer_flag_keeps_turn_busy_until_tasks_empty]
- AC4: a hook allow cannot override the built-in defer and a hook block wins over it [evidence: test:tests/test_stop_gate.py::test_hook_cannot_allow_past_builtin_defer]
- AC5: a deferred turn still ends as timeout at turn_max_seconds [evidence: test:tests/test_stop_gate.py::test_deferred_turn_hits_turn_max_seconds]
- AC6: a deferred turn can be interrupted [evidence: test:tests/test_stop_gate.py::test_deferred_turn_can_be_interrupted]
- AC7: deny is not honoured on onStop [evidence: test:tests/test_stop_gate.py::test_deny_not_honoured_on_on_stop]
- AC8: a command action exiting 2 on onStop blocks with its stderr [evidence: test:tests/test_stop_gate.py::test_exit_2_blocks]
- AC9: one turn_completed, one onTurnComplete and one callback per turn however many legs [evidence: test:tests/test_stop_gate.py::test_no_double_completion]
- AC10: stale and foreign Stops fire no onStop [evidence: test:tests/test_stop_gate.py::test_stale_or_foreign_stop_fires_no_on_stop]
- AC11: an agent-origin background Stop behaves as today and fires no onStop [evidence: test:tests/test_stop_gate.py::test_agent_origin_stop_unchanged]
- AC12: blocking onStop actions extend the relay timeout [evidence: test:tests/test_stop_gate.py::test_relay_timeout_covers_blocking_on_stop]
- AC13: README documents onStop, the new events and the config key [evidence: command:uv run pytest tests/test_readme_api.py tests/test_readme_config.py -q]

## Files

- `src/claude_code_wrapper/config.py`
- `src/claude_code_wrapper/bootstrap/settings.py`
- `src/claude_code_wrapper/hooks.py`
- `src/claude_code_wrapper/session.py`
- `src/claude_code_wrapper/router.py`
- `src/claude_code_wrapper/events.py`
- `README.md`
- `tests/test_stop_gate.py`

## Interfaces

- Part `on-stop-event`: `config.py::HOOK_EVENTS` (onStop), `HooksConfig.onStop`, `SessionsConfig.defer_stop_on_background_tasks`, `hooks.py::EVENT_DECISIONS["onStop"]`, `session.py::Session.stop_target`, events `turn_continued`, `turn_deferred`.
- Relay output for a block: stdout `{"decision": "block", "reason": R}`, exit 0.

## Expected outcome

A turn ends only when claude stops AND the gate allows it: hooks can send claude back to work with a reason, or hold the turn open while background tasks finish, with no double completion and every existing bound (stall, turn_max_seconds, interrupt) still in force.

## Steps

- Write `tests/test_stop_gate.py` with the fake's Stop block, `stop` lists and background steps (`world` fixture); see it fail.
- Factor `stop_target` out of `complete`; add config and decisions; implement the three outcomes in `_on_stop`; add the events; add onStop to `_GATED_EVENTS`.
- README sections and config row.
- `uv run pytest tests/test_stop_gate.py tests/test_router.py tests/test_session_turns.py tests/test_bootstrap_settings.py tests/test_config.py tests/test_readme_api.py tests/test_readme_config.py -q && mise run lint`.

## Notes

- Upstream: Stop input has `background_tasks` and `session_crons` since 2.1.145; teammates do not fire the parent Stop (they fire TeammateIdle), so only the lead's Stop is gated.
- Keep `self._usage` across legs: reset only when the turn really completes.
- A deferred turn keeps `Session._turn`, so `_watch_turn` and `interrupt` keep their target; check no path ends it twice (`_end_turn` is the single terminal transition).
