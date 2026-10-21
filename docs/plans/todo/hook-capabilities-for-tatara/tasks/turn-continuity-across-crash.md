---
title: Turn continuity across crash
status: todo
repo: .
value: "A claude crash mid-turn no longer loses the turn when a continue prompt is configured (K12)"
files: [src/claude_code_wrapper/config.py, src/claude_code_wrapper/session.py, src/claude_code_wrapper/router.py, src/claude_code_wrapper/events.py, README.md, tests/test_turn_continuity.py]
verify: "uv run pytest tests/test_turn_continuity.py tests/test_session_turns.py tests/test_e2e_resume.py tests/test_readme_api.py tests/test_readme_config.py -q && mise run lint"
depends_on: [passthrough-env-globs]
owns: []
uses: [fake-claude-scenarios, ccw-world-fixture]
model: opus
estimate: 90
actual: ""
review: full
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Turn continuity across crash

## Requirement

K12 turn continuity across a mid-turn crash (design D16).

- `sessions.restart_continue_prompt: str = ""` (empty = off, today's behaviour).
- When it is set and claude exits mid-turn with restarts left (`Session._on_exit`), the running record stays `running`: no `claude_exited` failure, no onTurnComplete, no callback. The session goes booting as today.
- After the relaunch boots resumed (`Session._recover`, transcript present), the continue prompt is pasted with the SAME `<!-- ccw turn: <turn_id> -->` marker, the session is busy again with the same record, and a `turn_resumed` event `{turn_id, restart}` is emitted. The turn completes on the resumed claude's Stop carrying that marker, with usage accumulated across the crash (the router's usage totals are not reset).
- The turn fails with `claude_exited` exactly once, as today, when: the restart budget is exhausted (dead), the relaunch fails to boot, or the relaunch is fresh (no transcript, nothing to continue).
- `limits.turn_max_seconds` and the stall watch keep running across the crash (they measure from the original start); `interrupt` during recovery and `stop`/destroy during recovery end the turn (`interrupted` / `session_stopped`) and nothing is pasted afterwards.
- `turn_resumed` joins `EVENT_TYPES`.
- README: the config row, `### \`turn_resumed\`` event, and the crash behaviour under the turns section.

## Acceptance conditions

- AC1: a mid-turn crash continues the same turn after the resumed relaunch and it completes [evidence: test:tests/test_turn_continuity.py::test_crash_mid_turn_continues_same_turn]
- AC2: the continue prompt carries the same turn marker [evidence: test:tests/test_turn_continuity.py::test_continue_prompt_carries_same_marker]
- AC3: with the budget exhausted the turn fails with claude_exited exactly once [evidence: test:tests/test_turn_continuity.py::test_budget_exhausted_fails_turn_once]
- AC4: with the option empty a crash fails the turn as today [evidence: test:tests/test_turn_continuity.py::test_off_by_default_fails_as_today]
- AC5: a destroy during recovery fails the turn with session_stopped and pastes nothing [evidence: test:tests/test_turn_continuity.py::test_destroy_during_recovery_fails_turn]
- AC6: turn_max_seconds spans the crash [evidence: test:tests/test_turn_continuity.py::test_turn_max_seconds_spans_crash]
- AC7: a turn_resumed event is emitted with the restart count [evidence: test:tests/test_turn_continuity.py::test_turn_resumed_event]
- AC8: usage is accumulated across the crash [evidence: test:tests/test_turn_continuity.py::test_usage_spans_crash]
- AC9: README documents the key and the event [evidence: command:uv run pytest tests/test_readme_api.py tests/test_readme_config.py -q]

## Files

- `src/claude_code_wrapper/config.py`
- `src/claude_code_wrapper/session.py`
- `src/claude_code_wrapper/router.py`
- `src/claude_code_wrapper/events.py`
- `README.md`
- `tests/test_turn_continuity.py`

## Interfaces

- `SessionsConfig.restart_continue_prompt`; event `turn_resumed`; `Session._on_exit` / `Session._recover` continuity path.

## Expected outcome

With a continue prompt configured, a claude crash mid-turn resumes the same turn after the relaunch and it completes normally; without it, nothing changes.

## Steps

- Write `tests/test_turn_continuity.py` with the fake's `exit_once` step (`world` fixture); see it fail.
- Keep the record running across `_on_exit` when continuing; paste the continue prompt with the same marker after a resumed boot in `_recover`; fail once on every non-continuable path.
- README.
- `uv run pytest tests/test_turn_continuity.py tests/test_session_turns.py tests/test_e2e_resume.py tests/test_readme_api.py tests/test_readme_config.py -q && mise run lint`.

## Notes

- `_end_turn` is the single terminal transition; `stop()` ends a running turn with session_stopped, keep that true during recovery.
- The router's `_last_marker` must match the continued turn so its Stop completes it.
