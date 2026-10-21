# Hook capabilities for tatara and magic-trainer parity record

## Summary

Asked: generic ccw hook capabilities K1-K13, three defect fixes (DEF-1..3) and worked tatara and magic-trainer examples, so both projects can move onto ccw without project-specific core code.
Shipped: 21 planned tasks plus 1 discovered flake task (tests-test-session-boot-py-t) and 1 epic-review fix task (epic-review-fixes) = 22 tasks, all `done` and merged directly into main (last merge a9ec17b). 99 files changed, +10779/-373 (645599a..a9ec17b).
Verdict: pass (epic review 2026-10-21). Full suite 870 passed, 7 deselected on 76f69b1; integration 7 passed; lint clean; pre-commit 9 hooks passed. Scope audit (auditOnly run wf_7aeb0c67-976): no gaps.
Four tasks needed hand verification because the verifier failed; the task files, not the run returns, hold the rulings. Several fix rounds added AC-named tests after the code (process gap, accepted).

## Planned vs built

Verifier head = `tasks[].verification.head` from the last plan-execute run that touched the task (runs 10-13). "Rounds" = review/fix rounds in that run. Continuations: not in the returns, unknown. Files: the `files:` list in each task file (the return's `filesChanged` only lists the last round).
Verification tails are the verifier's evidence tail. TDD: the verifier's tdd-revert result is not red evidence and is not quoted; red evidence is the implementer's `tests.red` where one was reported, otherwise "none reported".

| Task | Status | Rounds | Files | Verifier head | Verification tail / red |
|---|---|---|---|---|---|
| fake-claude-hook-scenarios | done | 3 | tests/fake_claude/fake_claude.py, tests/conftest.py, tests/test_fake_claude.py, tests/test_manager.py | a3d4431 | pass; lint "151 files already formatted". red (implementer): `git show 3187d73:tests/fake_claude/fake_claude.py > ...; uv run pytest tests/test_fake_claude.py -q` |
| hook-result-fields | done | 2 | hooks.py, router.py, README.md, tests/test_hook_result_fields.py | 2056e56 | pass; lint "152 files already formatted"; red none reported |
| pretooluse-gate-fails-closed | done | 2 | hooks.py, router.py, manager.py, README.md, tests/test_pretooluse_fail_closed.py | 6873e13 | pass; AC1-AC9 met; red none reported |
| busy-check-before-turn-files | done | 2 | session.py, router.py, tests/test_submit_busy_first.py | bc87100 | pass; AC1-AC5 met; implementation commit 2028f78 precedes tests, no red run |
| hook-context-env | done | 2 | hooks.py, manager.py, service.py, README.md, tests/test_hook_context_env.py | 97ff8c1 | pass; lint "154 files already formatted"; live CCW_API_URL under run_service port 0 not tested (cannotVerify) |
| stop-gate-onstop-event | done | 1 | config.py, bootstrap/settings.py, hooks.py, session.py, router.py, events.py, README.md, tests/test_stop_gate.py | 9bb8706 | pass; lint "156 files already formatted". red: `uv run pytest tests/test_stop_gate.py -q` -> "hooks.onStop Extra inputs are not permitted" |
| turn-annotations-from-hooks | done | 2 | turns.py, router.py, session.py, README.md, tests/test_turn_annotations.py | b1cb4fa | pass; implementer wrote code before tests, no red (process gap) |
| agent-attribution-and-usage | done | 2 | router.py, events.py, README.md, tests/test_agent_attribution.py | a3a894b | pass; tests added after existing impl; implementer's mutation check made 4 new tests fail; no red |
| turn-metadata-on-submit | done | 2 | api/turns.py, router.py, session.py, turns.py, README.md, tests/test_turn_metadata.py | 82111ae | pass; no red output captured (process gap) |
| ontick-timed-hook | done | 2 | config.py, hooks.py, router.py, README.md, tests/test_on_tick.py | bfd57c9 | pass; red none reported |
| probe-token-and-answer | done | 2 | config.py, router.py, session.py, turns.py, api/turns.py, README.md, tests/test_probe_token.py | 9b395f8 | pass; lint "161 files already formatted"; red none reported |
| outstanding-subagent-count | done | 2 | router.py, manager.py, README.md, tests/test_outstanding_subagents.py | be7b6e4 | pass; "37 passed", lint clean; red none reported |
| passthrough-env-globs | done | 2 | config.py, session.py, manager.py, README.md, tests/test_passthrough_globs.py | f23b9ba | pass; lint "163 files already formatted"; red none reported |
| turn-continuity-across-crash | done | 2 | config.py, session.py, router.py, events.py, README.md, tests/test_turn_continuity.py | ff97716 | pass; AC1-AC9 met. red (implementer): `pytest tests/test_turn_continuity.py -q -k off_by_default` with restart_continue_prompt=CONTINUE |
| destroy-hook-input-output | done | 1 (run 10, verify-agent-died), 2 (run 11) | api/sessions.py, manager.py, router.py, events.py, README.md, tests/test_destroy_io.py | 3effba5 (run 11; run 10 had no head) | pass; AC1-AC8 and AC7a met; tests added after existing impl, no red. Run 10 verifier died; rerun in run 11 passed |
| missing-metric-families | done | 3 | metrics.py, api/app.py, api/turns.py, router.py, README.md, tests/test_metric_families.py | 20a0889 | pass; lint "166 files already formatted". red (implementer): `pytest tests/test_metric_families.py -q` -> FAILED test_probe_to_dead_session_counted_rejected |
| service-exit-policy | done | 2 | config.py, manager.py, service.py, cli.py, README.md, tests/test_exit_policy.py | d2b583c | pass; lint "166 files already formatted"; red none reported |
| tests-test-session-boot-py-t | done | 2 | tests/test_session_boot.py:555 | ee159c2 | pass (stopped gate-repeat on tdd-skipped, flake fix has no deterministic red) |
| tatara-example-turn-hooks | done | 3 | data/examples/tatara.config.yaml, data/examples/tatara/tatara_hooks.py, examples/tatara.config.yaml, README.md, tests/test_example_tatara.py | 37c838c (verification.passed = false) | hand-verified on 37c838c, see Deviations. red (implementer): `pytest tests/test_example_tatara.py -q` with example config + script removed -> FAILED |
| tatara-example-lifecycle | done | 2 | data/examples/tatara.config.yaml, data/examples/tatara/tatara_hooks.py, README.md, tests/test_example_tatara_lifecycle.py | e6153f9 | pass; AC6 heading found; clean; red none reported |
| magic-trainer-example | done | 2 | data/examples/magic-trainer.config.yaml, data/examples/magic-trainer/mtg_hooks.py, examples/magic-trainer.config.yaml, README.md, tests/test_example_magic_trainer.py | 68bac41 (verification.passed = false) | hand-verified on 68bac41, see Deviations; tests written after code, no red |
| epic-review-fixes | done | 3 | router.py, hooks.py, manager.py, data/examples/tatara/tatara_hooks.py, tests/test_epic_review_fixes.py, README.md | unknown (verification.head = "unknown": verify-task.py timed out, exit 144) -> `unverified` in the run | hand-verified on 76f69b1, see Deviations. red (implementer): 4 src files reverted to 6b5dcb4 -> 5 FAILED (pretooluse_empty_tool_name, failed_blocking_onstop_with_deny, relay_deny_before_slow_on_error, hook_state_dir_is_private, callback_refuses_empty_hmac_secret) |

Verifier-less or failed-verifier rows in the run returns (`unverified` lists): run 10 had service-exit-policy, missing-metric-families, tatara-example-turn-hooks, tatara-example-lifecycle, magic-trainer-example (dependency-failed on destroy-hook-input-output); all later verified or hand-verified, except as marked above.

### Approvals

Source: `approvals` in runs 10-13 and `approvals.json`. Run 13 returned an empty approvals list; statuses below are from the last run that listed each entry, then the hand verifications.

| Id | Text | Decisions | Tasks | Status |
|---|---|---|---|---|
| A | magic-trainer-facing hook capabilities: onStop stop gate (block/defer, background-task defer flag) K1, hook annotations on turn record K2, agent attribution and per-message usage events K5 | see approvals.json | fake-claude-hook-scenarios, hook-result-fields, stop-gate-onstop-event, turn-annotations-from-hooks, agent-attribution-and-usage | delivered (run 10) |
| B | Lifecycle and context capabilities: DELETE hook_input/hook_outputs/destroy_output K3, CCW_STATE_DIR and CCW_API_URL K4, turn metadata K6, onTick K7, exit policy K8 | see approvals.json | hook-result-fields, destroy-hook-input-output, hook-context-env, turn-metadata-on-submit, ontick-timed-hook, service-exit-policy | delivered (run 11; run 10 escalated on verify-agent-died and dependency-failed) |
| C | Generic core options: probe token/answer K9, subagent count K10, passthrough globs K11, turn continuity K12, metric families K13 | see approvals.json | fake-claude-hook-scenarios, probe-token-and-answer, outstanding-subagent-count, passthrough-env-globs, turn-continuity-across-crash, missing-metric-families | delivered (run 11) |
| D | Defect fixes DEF-1 PreToolUse fails closed, DEF-2 busy check before turn files, DEF-3 README hook env | see approvals.json | pretooluse-gate-fails-closed, busy-check-before-turn-files, hook-context-env | delivered (run 10) |
| E | Worked tatara and magic-trainer examples tested against the fake claude, plus README docs | see approvals.json | tatara-example-turn-hooks, tatara-example-lifecycle, magic-trainer-example | run 12 return: escalated (gate-repeat on a symlink artifact). All three tasks hand-verified AC-met and merged, so delivered; no entry cleared by the run itself |

## Deviations and rulings

- Hand verification, tatara-example-turn-hooks (37c838c). What: verify-task.py --base fffee6d run by hand; AC1-AC6 met, tdd failedOnBase true. Why: the run's verifier reported passedOnHead false and a dirty tree; the cause is verify-task's TDD revert restoring the committed symlink examples/tatara.config.yaml as a regular file, which breaks the samefile ship check. With the symlink restored: targeted tests pass, lint clean, tree clean. Cost if wrong: an example that fails to ship via the symlink; covered by test_examples.py in the final suite (870 passed).
- Hand verification, magic-trainer-example (68bac41). What: verify-task.py --base e6153f9 without --tdd-revert; AC1-AC7 met, clean, no debt. Why: same symlink artifact caused the gate-repeat. Process gap accepted: tests written after the example code. Cost: examples are config plus scripts exercised only by these tests, so a behaviour the tests miss is unchecked; a live `claude -p` review run was never exercised (cannotVerify).
- Hand verification, epic-review-fixes (76f69b1). What: verify-task.py --base 6b5dcb4 --tdd-revert; AC1-AC7 met, tdd failedOnBase/passedOnHead, clean, full suite 870 passed. Why: the run's verifier timed out on the 7 minute full suite (exit 144, `--prev-head unknown`). AC1-AC5 refs renamed to the fixer's tests. Process gap accepted: tests and fix in one commit, red proven by revert (implementer's tests.red above). Cost: none beyond the gap.
- Hand verification, destroy-hook-input-output. What: rerun in run 11 passed (head 3effba5). Why: run 10 verifier died (verify-agent-died). Cost: none.
- Dropped discovered task (30819c2, "drop non-reproducible discovered task"). What: a task the run discovered during tatara-example-turn-hooks was dropped as not reproducible. Why: it could not be reproduced on main. Cost if wrong: an unfixed flake; the one flake that did reproduce is tests-test-session-boot-py-t (kept, done).
- Builder checks (design.md "Left to the builder"): magic-trainer-example depends on tatara-example-lifecycle (both edit README `## Hook examples`); tatara-example-lifecycle AC6 doc-section heading is created by the requirement; onStop annotations merge into the turn record on every leg (allow, block, defer); with exit_when_last_destroyed the DELETE returns 200 with full body (hook_outputs included) before exit 0; hook_outputs collects only blocking onDestroy actions (magic-trainer review sets blocking: true); CCW_CLAUDE_CONFIG_DIR and CLAUDE_CONFIG_DIR resolve to the same path; EVENT_DECISIONS covers existing events, onStop and onTick add their own. Cost if wrong: README and tests would need to be re-aligned per item.
- Epic review dismissals: body read before size cap on DELETE/turn (authenticated callers only), /metrics unauthenticated (by design, trust model), HMAC timestamp (would break the tatara operator contract). Two branch-topology notes from the scope audit (main contains all code). Cost if wrong: a hardening gap for unauthenticated /metrics scrapes or replayed callbacks.
- Merge-time fixes by the orchestrator on main: 93fca74 (stale world import, turn_resumed/usage event type pins), 4b1d358 (EVENT_TYPES pin for destroy_output). Cost: none, test-only.
- forwardMerged: none in any of runs 10-13. Declined findings that the implementers rejected as verifier artifacts (agent-attribution AC1-AC6, busy-check AC1-AC4, destroy-io AC1-AC7a pinned to api/sessions.py, symlink artifacts) were each re-checked by the implementer against the diff and tests; none was ruled by the orchestrator beyond the entries above.

## Duplicates reconciled

none. Run 10 process note: the e2e fix 51eb5d7 (onTick record hook interval_seconds) was flagged as possibly duplicated on other branches; integration kept one copy (merge 96b7590).

## Discovered and blocked

- tests/test_session_boot.py::test_restart_budget_exhausted_dead flaky (asserted rec.exits == [3] and got []). Task tests-test-session-boot-py-t created and done (waits on on_exit, ee159c2); merged as 0ac9ffc.
- Reported: `mise run lint` failing in the verifier environment on an unset ruff shim. Ruled not a repo defect and dropped (design.md builder ruling): the repo's lint task already runs `uv run ruff` and passes in every worktree; the error came from the verifier running bare ruff.
- cannotVerify, not run by anyone (no ruling settled them): live SIGTERM share (25 percent of shutdown_timeout_seconds) for onDestroy under a real service process; service-exit-policy 0.2s EXIT_STOP_DELAY_SECONDS timer under load; live ASGI/SSE behaviour of the metrics middleware and ccw_http_panics_total (only TestClient); live CCW_API_URL in a container; real tatara pod and Go bootstrap parity (credential helpers, sparse checkouts) and real exit_when_dead/restart_continue_prompt (tests check config values only); live `claude -p` magic-trainer review. Who can clear: the tatara and magic-trainer owners on first adoption.
- Open question from declined: HTTPS push authentication in tatara safety-push (bootstrap passes GIT_TOKEN only via per-process GIT_CONFIG_* env, so safety-push has no persisted credential). Not required by the task; tatara owner to confirm.
- No blocked or undelivered items in any run return.

## Research that changed the plan

Parity research 2026-10-21 (design.md) produced K1-K13 and DEF-1..3; the user decided gaps close with generic hooks, not ccw core. Upstream facts that shaped tasks: Stop input has background_tasks and session_crons since claude 2.1.145 and teammates do not fire the parent Stop (stop-gate-onstop-event); agent_id/agent_type are in Pre/PostToolUse inside subagents (2.1.69) and teammates (2.1.290) (agent-attribution-and-usage); Stop block cap 8 via CLAUDE_CODE_STOP_HOOK_BLOCK_CAP (2.1.143) (fake-claude-hook-scenarios). Design D12 (exit policy not a hook: `kill -TERM $PPID` gives the wrong code and races the drain) shaped service-exit-policy.

## Cost

Agents by role, continuations and wall-clock per run: unknown (not in the returns given). Runs: 10, 11, 12, 13 plus an auditOnly run; each returned `rounds: 1` at the workflow level, and `audit: "off"` in tasks runs. Review rounds per task are in the table.

README Time: Estimate 1440 min, Actual 96 min, Remaining estimate 60 min.

| Task | Estimate (min) | Actual (min) |
|---|---|---|
| fake-claude-hook-scenarios | 75 | 12 |
| hook-result-fields | 60 | 2 |
| pretooluse-gate-fails-closed | 60 | 4 |
| busy-check-before-turn-files | 45 | 3 |
| hook-context-env | 60 | 6 |
| stop-gate-onstop-event | 90 | 20 |
| turn-annotations-from-hooks | 45 | 5 |
| agent-attribution-and-usage | 60 | 1 |
| turn-metadata-on-submit | 60 | 2 |
| ontick-timed-hook | 75 | 5 |
| probe-token-and-answer | 60 | 7 |
| outstanding-subagent-count | 45 | 1 |
| passthrough-env-globs | 60 | 3 |
| turn-continuity-across-crash | 90 | 15 |
| destroy-hook-input-output | 75 | 10 |
| missing-metric-families | 75 | unknown |
| service-exit-policy | 75 | unknown |
| tests-test-session-boot-py-t | unknown | unknown |
| tatara-example-turn-hooks | 90 | unknown |
| tatara-example-lifecycle | 90 | unknown |
| magic-trainer-example | 90 | unknown |
| epic-review-fixes | 60 | unknown |

## MRs and deploy

No MR or PR: task branches were merged directly into main (last merge a9ec17b). Pushed to GitHub after this record. No deploy; library/service change only, no pipeline state read.

## Lessons routed to

- Verifier TDD revert turns committed symlinks (examples/*.config.yaml) into regular files and fails the dirty-tree and samefile checks (hit on tatara-example-turn-hooks and magic-trainer-example). Check to add: a verify-task.py test that reverts non-test files with `git checkout <base> -- <path>` (preserving mode 120000), plus a regression test using a symlinked fixture. Mechanical.
- verify-task.py full-suite run exceeds the agent's 120s foreground limit and returns no JSON (exit 144, `--prev-head unknown`, hit on epic-review-fixes and destroy-hook-input-output). Check to add: run verify-task.py detached and read its output file, and a lint in plan-execute that rejects `--prev-head unknown` as a gate-repeat input. Mechanical.
- Tests written after code (turn-annotations, turn-metadata, agent-attribution, destroy-io, both examples, epic-review-fixes). Check to add: a gate that requires the implementer commit tests before the implementation commit when the task is a feature (hook on the task branch checking git log order). Mechanical.
- `mise run lint` in the verifier fails on an unset ruff shim. Check to add: a mise.toml pin for ruff or a lint task that runs `uv run ruff`, with a pre-flight in the verifier that fails with a clear message. Mechanical.
- Whether a security finding is dismissed (unauthenticated /metrics, HMAC timestamp). Judgement call: edit the epic review prompt in the start-development skill to require each dismissal to be recorded with its reason in the task file.
