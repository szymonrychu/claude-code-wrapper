# claude-code wrapper microservice record

## Summary

Asked: a standalone Python microservice (`ccw`) running N interactive Claude Code sessions under a PTY, with turns API, SSE, config.yaml-driven setup, typed Claude env, hooks, README, container + compose, a fake-claude e2e, and (mid-run) pip packaging.
Shipped: 34 task files, all `done`, on local main 5d980a3 (19 approvals A1-A19, all delivered). Final verification on 5d980a3: `603 passed, 1 warning in 215.56s`; lint `All checks passed! 122 files already formatted`; docker build and `ccw check-config --config /app/config.example.yaml` exit 0.
Verdict: pass (epic review 2026-10-20). Three tasks (claude-env-model, e2e-lifecycle, pip-package) have no workflow verifier head because plan-execute passed `--prev-head unknown`; the orchestrator re-verified them by hand (see rulings).
Plan-execute ran 7 times (runs 1-5, 7 and remediation run 6) plus one auditOnly run.

## Planned vs built

Runs: run1 wf_a0e975da-8d0, run2 wf_88b6b222-969, run3 wf_5224994d-9ea, run4 wf_1c0f6e8f-0e9, run5 wf_897d2f82-730, run6 (remediation) wf_6cc5d3fc-4ff, run7 wf_202f0e91-4b0, audit wf_ff8ba4b1-478.

Rounds are summed over runs where a task ran twice. Files = `filesChanged` from the task's last run (a per-run delta; 0 for session-turns although its commits exist, so it is not a total). Continuations: unknown (not in the returns I was given). Verifier head = `tasks[].verification.head`. Red = implementer's own `tests.red` (no tdd-revert quoted); "none recorded" = no `tests.red` in the return.

| Task | Status | Rounds | Files | Verifier head | Red (implementer run) |
|---|---|---|---|---|---|
| repo-scaffold | done | 1 | 8 | f89bd241 | 6 failed in test_scaffold.py |
| pty-process | done | 1 | 4 | c1ab4d3a | ModuleNotFoundError `claude_code_wrapper.ring` |
| fake-claude | done | 2 | 1 | 9e8bb0d4 | none recorded |
| hook-relay | done | 2 | 1 | dae5b4ea | none recorded |
| source-analysis | done | 1 | 1 | 0d25f5af | none recorded (doc) |
| claude-env-model | done (ruled) | 3, gate-repeat | 0 | UNVERIFIED (head `unknown`); orchestrator re-verify on 922b155 | restore of b25ce46 `claude_env.py` then test_claude_env.py FAILED |
| event-log | done | 3 (round-cap) + 2 | 1 | 065f2627 (run1), 1e097790 (final) | `test_corrupt_tail_mid_multibyte` UnicodeDecodeError |
| config-models | done | 3 | 1 | 85e81dcb | none recorded |
| event-router | done | 3 | 1 | 0d1b3040 (pre-rebase; ruling 6: verified_head now rebased tip bdd80af) | none recorded |
| home-seed | done | 1 | 5 | 73639bb8 | ImportError `MARKER` from bootstrap |
| hook-dispatcher | done | 2 | 3 | 18169cfd | `test_webhook_total_time_bounded_by_timeout` |
| plugins-and-mise | done | 2 (no-progress) + 2 | 1 | 42cb214b (run2), b848b1cf (final) | `test_subprocess_stdin_is_closed` |
| session-boot | done | 1 | 2 | 9465f23e | ModuleNotFoundError `claude_code_wrapper.session` |
| session-turns | done | 3 | 0 | 6ded9e6e | scratch copy with 4fb062f session.py/turns.py, 2 targeted tests failed |
| settings-render | done | 2 | 1 | 30752bd2 | none recorded |
| transcript-tailer | done | 2 | 1 | 3a16ca33 | none recorded |
| workdir-init | done | 3 | 2 | 13910b56 | IsADirectoryError in test_workdir.py |
| session-manager | done (ruled) | 3 (round-cap) | 6 | b19eb960 | shutdown_stops_all, shorter_grace, close_drain_is_bounded failed |
| api-sessions | done | 2 | 3 | c053cbe3 | none recorded |
| api-turns-events | done | 2 | 2 | c557bf54 | none recorded (reviewer: tests written after implementation, declined) |
| internal-api | done | 2 | 1 | d2028acb | none recorded |
| metrics | done | 2 | 1 | cfe0199e | none recorded |
| service-main | done | 2 | 3 | 900a8e89 | none recorded |
| container | done | 2 | 3 | 655e4d3e | none recorded |
| workbench-example | done | 2 | 1 | 920fee4c | none recorded |
| e2e-lifecycle | done (ruled) | 3 (round-cap) | 0 | UNVERIFIED (head `unknown`); orchestrator re-verify on 415c680 | none recorded |
| readme-api | done | 1 | 2 | 5a4794b5 | FileNotFoundError README.md |
| readme-config | done | 2 | 1 | a1bf8fee | none recorded |
| pip-package | done (ruled) | 3 (gate-repeat) | 2 | UNVERIFIED (head `unknown`); orchestrator re-verify on 1cca7a2 + docker build smoke | `test_example_config_has_no_hardcoded_container_paths` |
| audit-fixes-container-home-s | done | 2 | 1 | 648ce095 | none recorded |
| review-fixes-auth-compose | done | 2 | 3 | ba14bccb | none recorded |
| review-fixes-dedupe-helpers | done | 0 in run6 (verify-agent-died, no head: unverified) + 2 in run7 | 3 | a88682a7 (run7) | run6 `tests.red` for test_timestamps present; run7 none |

Verification tail: every task with a head shows `verification.passed: true` in its last run. The three `unknown` rows have `passed: false` in the return; the failure was the harness error `git diff ... unknown HEAD: fatal: ambiguous argument 'unknown'`, not a test result. Orchestrator re-verified output: verify-task.py on HEAD with --tdd-revert passed, `failedOnBase: true` (scratchpad `verify-env-model.json`, `verify-e2e.json`, `verify-pip.json`); that tdd-revert is a harness check, not red evidence. Run 1 dependency-failed tasks (cascade from claude-env-model/event-log escalation) were rebuilt in runs 2-5.

Files vs summaries: task files have `actual` blank for 20 of 34 tasks (summary Time total 67 min is partial). run6 returned `verified: true` for review-fixes-dedupe-helpers with no head; treated as unverified until run7.

### Approvals

Final status is from the last run that touched each entry plus the audit (16/19 landed at first; A2, A5, A6 were gaps, fixed by audit-fixes-container-home-s). The returns for runs 1-3 list these as `escalated` only because of cascade failures; all were later delivered. A11 additionally re-delivered by review-fixes-auth-compose.

| Id | Text (short) | Decisions | Tasks | Status |
|---|---|---|---|---|
| A1 | Python microservice extracted from tatara, magic-trainer, ros2 | D1 D3 D13 D17 | repo-scaffold, pty-process, session-boot, session-turns, session-manager, service-main | delivered |
| A2 | Compatible with workbench project | D11 | workbench-example, settings-render, event-router, workdir-init, home-seed, plugins-and-mise, container, audit-fixes-container-home-s | delivered (gap fixed in audit-fixes) |
| A3 | TDD methodology + fake-claude lifecycle e2e | D15 | fake-claude, e2e-lifecycle | delivered (e2e orchestrator-verified) |
| A4 | README covering endpoints, SSE events, hooks | D16 | readme-api, readme-config | delivered |
| A5 | Container + compose with workdir/sessions volumes | D4 D10 | container, session-manager, audit-fixes-container-home-s | delivered (gap fixed in audit-fixes) |
| A6 | config.yaml: init, prompts, mise, plugins | D8 D12 D14 | config-models, workdir-init, plugins-and-mise, home-seed, session-manager, api-turns-events, service-main, audit-fixes-container-home-s | delivered (gap fixed in audit-fixes) |
| A7 | Definable hooks (full event set) | D6 D7 | hook-dispatcher, hook-relay, settings-render, internal-api, event-router, transcript-tailer | delivered |
| A8 | Source analysis doc | D16 | source-analysis | delivered |
| A9 | Typed Claude env vars, extra, override, redaction | D2 | claude-env-model, config-models, session-manager, api-sessions | delivered (claude-env-model orchestrator-verified) |
| A10 | Config docs: every key with type, default, env var | D2 D16 | readme-config | delivered |
| A11 | HTTP API: sessions, turns, interrupt, transcript, SSE, auth, metrics | D4 D5 D9 D18 | api-sessions, api-turns-events, event-log, internal-api, metrics, service-main, review-fixes-auth-compose | delivered |
| A12 | PTY-driven interactive claude, not SDK/-p | D1 | pty-process, session-boot, fake-claude | delivered |
| A13 | DELETE keeps dirs unless ?purge=true | D19 | session-manager, api-sessions | delivered |
| A14 | AskUserQuestion forwarded; signed callbackUrl; camelCase | D20 | event-router, api-sessions, api-turns-events, session-turns, session-manager, readme-api, e2e-lifecycle | delivered |
| A15 | Third named volume `mise` at /opt/mise | D21 | container | delivered |
| A16 | onTurnComplete for every terminal state | D22 | event-router, readme-api, e2e-lifecycle | delivered |
| A17 | Trust model: one instance per project, documented | D23 | readme-api, readme-config, api-sessions, api-turns-events | delivered |
| A18 | Secret env overrides never persisted | D24 | session-manager, api-sessions, readme-config | delivered |
| A19 | pip-installable package with command on PATH | D25 | pip-package | delivered (orchestrator-verified; not published to an index) |

## Deviations and rulings

1. claude-env-model, e2e-lifecycle, pip-package escalated (gate-repeat / round-cap) only because plan-execute passes `--prev-head unknown` after a verifier death (workbench plugin `workflows/plan-execute.js:1022`); verify-task crashed in `git diff` before running any test. Ruling: orchestrator re-ran verify-task.py on HEAD with --tdd-revert, all passed with failedOnBase true; marked done (rulings.json for claude-env-model: HEAD 922b155, base f89bd24). Cost if wrong: the three tasks lack a workflow-attested head; a defect the implementer-loop missed would reach main. Mitigated by the 603-test suite on main.
2. event-log round-cap: finding (rotation disabled after a corrupt multibyte tail) was real. AC12 amended, fixed in run 2 (1e097790). Cost if wrong: log rotation silently off after tail corruption.
3. plugins-and-mise no-progress: two missing-test findings were converted into ACs and fixed in run 3 (b848b1cf). Cost if wrong: unfixed closed-stdin and post-install check gaps.
4. session-manager round-cap: only tests-changed gates remained; additive tests in tests/test_pty_proc.py (`test_close_shorter_grace_escalates`) and tests/test_router.py (`test_close_drain_is_bounded`) judged legitimate for AC13, accepted (verified on b19eb96). Cost if wrong: weakened tests hidden in other tasks' files; the diff was checked additive-only.
5. Discovered tasks dropped: the harness prev-head bug (outside this repo; recorded as process gap) and a duplicate of review-fixes-dedupe-helpers AC5 (38ea01c, 991bb21). Cost if wrong: the harness bug recurs in the next epic.
6. Rebased merges broke ticket.py merge detection (`_task_merged`); switched to plain `--no-ff` merges; event-router verified_head repointed to rebased tip bdd80af (a4a3737, e318347). Cost if wrong: a stale verified_head would misreport which SHA was verified.
7. Mid-run user additions: D2/A9 typed claude env (claude-env-model), k8s probes context, D25/A19 pip package (pip-package task added, b363e51). Cost: larger scope and the harness-bug exposure above.
8. run6 remediation of review-fixes-dedupe-helpers: verify-agent-died (no head); rerun in run7 gave a88682a. Declined in run 7: JSON log-line `ts` in service.py:54 still `+00:00` (task names turns, events, hooks only). Cost if wrong: mixed timestamp formats in logs only.
9. forwardMerged: none in any run. User decisions applied: D1 (PTY), D2, Q1-Q6 (D19-D24), D25.

## Duplicates reconciled

plan-execute `duplicates` was empty in every run and the audit reported 0. Epic review found overlap in main: duplicated timestamp helper (kept one `now_iso`, RFC3339 Z), duplicated `_squash`/`_out` (shared), duplicate example config (one kept, image ships `/app/config.example.yaml`); all fixed by review-fixes-dedupe-helpers (dbf38f8).

## Discovered and blocked

- Harness bug: plan-execute `--prev-head unknown` (workbench plugin, plan-execute.js:1022). Owner: workbench plugin maintainer (Szymon). Not cleared; worked around by manual verification.
- test_serve_preflight_missing_binaries broke after auth change (exits `server.auth_token: empty` first). Cleared in ea212a0 (preflight fixture passes auth policy; git-missing branch added).
- Not verified by any agent (declined/cannotVerify, unsettled by a ruling): behaviour against a real `claude` binary (dialog patterns, hook payload shapes, `claude plugin ... --json` output, onboarding/trust seeding); Linux behaviour of PTY close and process-group kill (tests ran on macOS); live Prometheus scrape; `docker compose up` smoke; gh/glab download checksums and base-image digest pin (noted only); package not published, `pip install claude-code-wrapper` from an index unverified; the 401-var env catalogue not compared to the live docs page. Whoever runs a real-Claude smoke (Szymon) can clear these.
- Repo has no remote; nothing blocked on review. Next epic github-ci-pipeline-with-harb handles CI and publishing.

## Research that changed the plan

- Source projects (tatara, magic-trainer, ros2, workbench, SDK docs, Claude env-var docs): briefs in docs/source-analysis.md drove the design; the live env-vars page (code.claude.com/docs/en/env-vars) shaped claude-env-model and A9/D2.
- Epic-review security review: empty auth_token refused unless `server.allow_anonymous`; compose requires a token and binds 127.0.0.1 (review-fixes-auth-compose, 9e8bb0d). Dropped by design (D23): /metrics unauthenticated, command hooks receive the service env.
- Scope audit (wf_ff8ba4b1-478): image lacked `/app/config.example.yaml` (container AC10) and home-seed tests lacked AC names; added audit-fixes-container-home-s (82371c7, 648ce09).

## Cost

Agents: run6 12 agents / 321,815 tokens; audit 1 agent / 149,018 tokens. Runs 1-5 and 7: agent counts, continuations and wall-clock unknown (not in the returns provided). Audits: one auditOnly scope audit plus the per-run scope audits. README Time: Estimate 2310 min, Actual 67 min (partial: 20 task files have blank `actual`), Remaining 0.

| Task | Est | Actual |
|---|---|---|
| api-sessions | 90 | unknown |
| api-turns-events | 90 | unknown |
| audit-fixes-container-home-s | 45 | unknown |
| claude-env-model | 75 | 3 |
| config-models | 90 | 2 |
| container | 75 | unknown |
| e2e-lifecycle | 90 | unknown |
| event-log | 60 | 1 |
| event-router | 90 | 4 |
| fake-claude | 90 | 4 |
| home-seed | 60 | 1 |
| hook-dispatcher | 90 | 2 |
| hook-relay | 45 | 1 |
| internal-api | 60 | unknown |
| metrics | 60 | unknown |
| pip-package | 60 | unknown |
| plugins-and-mise | 75 | 1 |
| pty-process | 60 | 6 |
| readme-api | 75 | unknown |
| readme-config | 75 | unknown |
| repo-scaffold | 45 | 1 |
| review-fixes-auth-compose | 60 | unknown |
| review-fixes-dedupe-helpers | 60 | unknown |
| service-main | 75 | unknown |
| session-boot | 90 | 12 |
| session-manager | 90 | unknown |
| session-turns | 90 | 20 |
| settings-render | 60 | 1 |
| source-analysis | 75 | 4 |
| transcript-tailer | 90 | 3 |
| workbench-example | 45 | unknown |
| workdir-init | 75 | 1 |

## MRs and deploy

No remote, no MR or PR, no pipeline, no deploy. Delivery is the local merge to main (5d980a3; ticket commits 50fe20d, 8447453, 8fb68af after). The repo will be pushed public by epic github-ci-pipeline-with-harb. Image builds locally (docker build and `ccw check-config` exit 0).

## Lessons routed to

- Mechanical: add a test in the workbench plugin for plan-execute.js that the verify-task invocation never receives the literal `unknown` for `--prev-head` (fall back to merge-base with main) and that a verify-task crash is reported as `harness-error`, not as code findings.
- Mechanical: add a ticket.py test for `_task_merged` on rebased versus `--no-ff` merges (rebased merges were undetected).
- Mechanical: add a pre-merge check that every AC evidence test name in a task file exists in the repo (grep `def <name>`); home-seed shipped with unmatched names and the audit caught it late.
- Mechanical: add a config test that a default config with empty `auth_token` fails to start (now present; keep as a regression guard) and that the example config validates inside the built image.
- Judgement: edit start-development to require plain `--no-ff` merges for epic tasks and to record a manual verify-task re-run when the workflow lacks a head.
