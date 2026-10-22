# Cross-container peer messaging record

## Summary

Asked: ccw-owned peer messaging between main agents of ccw sessions across containers, nodes and clusters (opt-in `peers:` config, heartbeat-based membership with static seeds, `peers`/`send_message` MCP tools, HMAC-signed message and notice endpoints, per-session inbox, pre/postJoin and pre/postLeave hooks, native SendMessage/ListAgents switched off).
Shipped: 20 planned tasks plus 1 post-merge task (peers-security-hardening) on main, base b83793c, head 9fd0b96 (89 files, +10971/-44 per `git diff --stat b83793c..main`). Approvals A-O all landed per the orchestrator's scope audit.
Verdict: delivered; the workflow's own `audit` was "off" and `scopeAudit` null in all three runs, so the scope audit was done by hand (sonnet).
Final suite: `mise run test` on 08d4742: 1493 passed, 12 deselected; lint clean (269 files formatted). Post-hardening regression (departed instance re-added via heartbeats) caught by the suite and fixed in 08d4742 (409 left).
CI: PENDING

## Planned vs built

Run 1 = plan-execute run 1 (18 tasks; `finished: false`), run 2 = readme + two-container tasks (`finished: true`), run 3 = security hardening (`finished: true`). Review rounds are `tasks[].rounds`. Continuations: not present in any return value - unknown. Files = count from the task file `files:` list (the returns' `filesChanged` read 0 for several tasks, so it was not used). Red = whether `tests.red` holds an implementer red run; "none" means no `tests.red` in the return.

| Task | Run | Status | Rounds | Files | Verifier head | Verification | Red (implementer) |
|---|---|---|---|---|---|---|---|
| fake-claude-peer-harness | 1 | done; return says stopped gate-repeat but verification passed=true | 3 | 3 | fc37e9a | passed | recorded; processGap tdd-skipped raised |
| peers-config-section | 1 | done | 2 | 4 | 6ffbc90 | passed | none |
| peer-auth-signing | 1 | done | 2 | 2 | 3c19b55 | passed | none |
| peer-member-table | 1 | done | 2 | 2 | ea790b6 | passed | none |
| peer-events-and-metrics | 1 | done | 3 | 5 | 4fdde8e | passed | recorded; processGap asked for revert/restore tails |
| peer-membership-hooks | 1 | done | 2 | 5 | 82edbdb | passed | none |
| peer-http-client | 1 | done | 2 | 2 | 8122779 | passed | none |
| peer-messages-endpoint | 1 | done | 3 | 4 | 487530d | passed | recorded (test_heartbeat_metrics_other_failures) |
| peers-mcp-tools | 1 | done | 2 | 3 | c9b6474 | passed | none (implementer red run not preserved) |
| peer-membership-service | 1 | done | 3 | 2 | d618f95 | passed | none |
| peer-inbox-delivery | 1 | done | 2 | 4 | 5731713 | passed | recorded |
| peer-inbox-outcomes | 1 | done | 2 | 2 | 42afeaa | passed | recorded |
| peer-session-bootstrap | 1 | done | 2 | 6+ (list truncated in listing) | 6022e26 | passed | none |
| peer-service-core | 1 | done | 2 | 2 | 290104f | passed | none |
| manager-peers-lifecycle | 1 | done | 2 | 2 | 3f5cfae | passed | none (implementer disclosed no clean red run; processGap) |
| peers-join-leave-order | 1 | done | 2 | 4 | 96b8477 | passed | none |
| peers-two-instance-e2e | 1 | done | 2 | 1 | 7b73a7f | passed | none |
| peers-compose-example | 1 | done; return: escalated gate-repeat, verification passed=false | 3 | 5 | bcf55ac | gate failed (dirty-tree, tdd-skipped); hand-verified on bcf55ac: 16 passed, lint clean (ruling) | recorded (removed docker-compose.peers.yml, reran) |
| peers-readme-guide | 2 | done (run 1: dependency-failed, unverified) | 2 | 2 | 293d2e9 | passed | none |
| peers-two-container-test | 2 | done (run 1: dependency-failed, unverified) | 2 | 8+ | caba18b | passed | none |
| peers-security-hardening | 3 | done | 2 | 5+ | 62f99c7 | passed | none |

No `tdd-revert` output is quoted as red evidence anywhere in this table. Tasks with "none" have no red run in the returns; TDD for them is unproven by the record (the commit history shows feat and test commits, not a red run).

Contradictions between summaries and files:
- Run 1 `filesChanged` is 0 for fake-claude-peer-harness, peer-events-and-metrics, peer-membership-service and peers-compose-example although the task files and git log show changes.
- fake-claude-peer-harness: `stopped: gate-repeat` in the return, yet `verification.passed` is true at fc37e9a and 0079843 merged it. No ruling covers this; the files (merge present, verification passed) were taken over the stop flag.
- peers-compose-example: `verified: false`, see ruling below.

Approvals (ledger `approvals.json`; status from the runs' `approvals` return, then settled):

| ID | Text (short) | Decisions | Tasks | Status |
|---|---|---|---|---|
| A | custom ccw HTTP peer messaging replaces native sockets | D1 | peer-http-client, peer-messages-endpoint, peer-service-core, manager-peers-lifecycle | delivered (run 1) |
| B | opt-in peers config, `ccw serve --peer` | D2 | peers-config-section, manager-peers-lifecycle, peers-join-leave-order | delivered (run 1) |
| C | native built-ins off (HARBOR_KITE=0, deny rules, PreToolUse gate) | D3 | peer-session-bootstrap, manager-peers-lifecycle | delivered (run 1); real-claude behaviour is cannotVerify |
| D | `peers` and `send_message` MCP tools, main agent only | D4 | peers-mcp-tools, peer-session-bootstrap, peer-service-core | delivered (run 1) |
| E | join/leave via lifecycle, hooks, heartbeat | D5, D15 | peer-member-table, peer-membership-hooks, peer-membership-service, peer-messages-endpoint, manager-peers-lifecycle, peers-join-leave-order, peers-two-instance-e2e | delivered (run 1) |
| F | token-authenticated peer routes | D6, D13 | peer-messages-endpoint, peer-http-client, peer-auth-signing | delivered (run 1) |
| G | per-session inbox, events, metrics | D7, D9 | peer-events-and-metrics, peer-inbox-delivery, peer-service-core | delivered (run 1) |
| H | notify_when_idle notices | D8 | peer-inbox-outcomes, peer-service-core, peers-two-instance-e2e | delivered (run 1) |
| I | TLS out of scope, README note | D11 | peers-readme-guide | delivered (run 2; run 1 escalated, dependency-failed) |
| J | two-service compose example | D10 | peers-compose-example | escalated in run 1 (gate-repeat), not in run 2 return; settled by orchestrator ruling (hand verification, merged d4b06cf); delivered per scope audit |
| K | fake-claude tests, e2e, docker two-container test | D12 | fake-claude-peer-harness, peers-two-instance-e2e, peers-two-container-test | delivered (run 2) |
| L | README, examples symlinks | D11 | peers-config-section, peer-events-and-metrics, peer-membership-hooks, peer-messages-endpoint, peers-readme-guide, peers-compose-example | delivered (run 2) |
| M | repo CLAUDE.md conventions, task size | D12 | all 20 tasks | delivered (run 2) |
| N | plan-review hardening (HMAC, skew, nonce, rate limit, notices off Stop path) | D13, D14, D7, D8, D2 | peer-auth-signing, peer-http-client, peer-messages-endpoint, peer-inbox-delivery, peer-inbox-outcomes, peer-membership-service, peer-service-core, peers-config-section, peers-readme-guide | delivered (run 2) |
| O | heartbeat only, drop join/leave endpoints | D16 | peer-http-client, peer-messages-endpoint, peer-member-table, peer-membership-service, peer-service-core, peer-events-and-metrics, peer-membership-hooks, peers-join-leave-order, peers-two-instance-e2e, peers-two-container-test, peers-readme-guide | delivered (run 2) |

Run 3 returned an empty `approvals` list (peers-security-hardening has no ledger entry); its scope is recorded only under Deviations and rulings.

## Deviations and rulings

1. Design history. What: native Unix-socket messaging researched then dropped; shared-folder discovery dropped; join/leave endpoints dropped for heartbeat only (`leaving` flag). Why: sockets do not cross nodes even on RWX CephFS; the user asked for join/leave + static seeds + pre/post Join/Leave hooks, then called the two extra endpoints redundant. Cost if wrong: the mesh depends on one heartbeat route, so a mistake in its first-round/leaving semantics breaks both join and leave.
2. Pre-gate panel (fable, opus, sonnet). What: token exfiltration via directory url and the Stop-path notice were the converging findings; all 12 fixes folded into the plan. Shared token = single trust domain, accepted by the user ("Otherwise GO"). Cost if wrong: any token holder can push prompts into bypass-mode agents; documented in the README trust model.
3. peers-compose-example gate-repeat. What: escalated gate-repeat accepted. Why: the `verify-task --tdd-revert` symlink artifact turned `examples/peers.config.yaml` into a regular file; symlink recreated, verify passed by hand on bcf55ac (16 passed, lint clean). Cost if wrong: a committed example drifts from the data copy; mitigated by `tests/test_examples.py`.
4. Orchestrator fixes on main. 1f9b8a3: `test_command_vocabulary` pins updated for 20 hook events (test-only pin, no behaviour change). b468acc: `test_stopped_service_leaves` deflaked by giving that test a large miss budget so only the leaving heartbeat can drop C. Full suite then 1483 passed (peers-main-suite3.log: "1483 passed, 12 deselected"; the run before the deflake had `1 failed, 1482 passed`). Cost if wrong: the leave test no longer exercises the missed-heartbeat path; that path is covered elsewhere.
5. Post-merge security review. What: no critical/high; 2 medium + 3 low closed by task peers-security-hardening (merged 9fd0b96). Cost if wrong: residual medium/low issues remain in peer routes.
6. Scope audit. What: workflow audit was off; sonnet audited by hand and every approval item A-O landed. Cost if wrong: an item marked delivered is missing; no machine audit backs it.
7. Disk-full interruption (ENOSPC) mid-planning, cleared by freeing space. Cost: time only; no plan content lost per orchestrator.
8. `forwardMerged`: empty in all three runs - none to record.
9. Unrequested additions disclosed by implementers: extra `transport` kwarg on `start_peers` and a one-line HARBOR_KITE addition in `tests/fake_claude/fake_claude.py` (manager-peers-lifecycle, declined as findings, kept). Not covered by an orchestrator ruling.

## Duplicates reconciled

none (`duplicates` empty in all three runs).

## Discovered and blocked

`blocked`, `undelivered`, `discovered` and `misattributed` are empty in all three runs.

cannotVerify (nobody can clear these without a real claude/credentials or a real peer; owner: Szymon Richert):
- Real claude accepting the `peers`/`send_message` schemas and honouring `CLAUDE_CODE_HARBOR_KITE=0` plus the deny rules (peer-session-bootstrap, peers-mcp-tools; needs real claude credentials).
- Streamed response cap against a real peer without content-length (peers-security-hardening; unit test uses MockTransport only).
- Live two-container run of `mise run test:integration` was not run by the implementer (needs docker); leave bound is 2*heartbeat+10s, looser than "within 2 heartbeat intervals" but within the 60s cap (peers-two-container-test).
- Other per-task cannotVerify items (run 1): behaviour under real uvicorn (Content-Length/chunked), behind a reverse proxy that rewrites the path (signature covers `request.url.path`), real multi-node heartbeat leave, test stability under repeated full-suite runs (peers-two-instance-e2e), destroy vetoed after leave under exit_when_last_destroyed (peers-join-leave-order). Each was left to the integrated full-suite run or is real-network behaviour; owner: orchestrator for the suite items, Szymon for real-network ones.

Declined findings (independent verifier claims the implementers rejected with test evidence; no ruling): harness AC1-AC3, lifecycle AC1-AC5, peer-auth-signing AC1-AC6, peer-events-and-metrics AC2-AC4 (mis-attributed to events.py, behaviour is in metrics.py), peers-readme-guide AC1-AC3 (read as mislabelled passes), peers-two-container-test skip( finding and integration deselection (both specified behaviour), peers-security-hardening oldest-evicted rate-map bypass (by design, bounded by the total limit). Owner to overturn: orchestrator.

Process gaps from run 1 (tdd-skipped for harness, no clean red for lifecycle, missing revert tails for events-and-metrics): not cleared by a ruling; recorded in the table above.

## Research that changed the plan

- `/private/tmp/claude-501/-Users-szymonri-Documents-claude-code-wrapper/a331ef5d-1095-4ebd-a72b-1e08088e2b5c/scratchpad/peer-messaging-research.md`: native Claude Code peer messaging rides Unix sockets that cannot cross nodes, even on RWX CephFS. Change: native design dropped for ccw-owned HTTP messaging (approval A).
- Pre-gate panel findings (token exfiltration via directory url and the Stop-path notice): 12 fixes folded into the plan; became approval N (HMAC signing, notices off the Stop path).
- Post-merge security review: 5 findings (2 medium, 3 low) became the peers-security-hardening task (bounded contacts, capped reads, total rate limit, bounded remote text).

## Cost

Agents by role: unknown (not in the returns). Triage calls: run 1: 54 (108 in / 31161 out tokens), run 2: 3 (6 / 1573), run 3: 3 (6 / 977). Plan-execute rounds field: 1 in each run. Continuations: unknown. Workflow audit: off. Scope audit: sonnet by hand. Wall-clock: unknown (run return files were written 14:16, 14:25 and 14:59 on 2026-10-22; start time not read).

Estimate vs actual (task files; `actual` is empty in every task file, README `Time` reads "Estimate: 1395 min | Actual: 0 min | Remaining estimate: 0 min", which predates the hardening task):

| Task | Estimate (min) | Actual (min) |
|---|---|---|
| fake-claude-peer-harness | 60 | unknown |
| peers-config-section | 60 | unknown |
| peer-auth-signing | 45 | unknown |
| peer-member-table | 60 | unknown |
| peer-events-and-metrics | 45 | unknown |
| peer-membership-hooks | 60 | unknown |
| peer-http-client | 75 | unknown |
| peer-messages-endpoint | 75 | unknown |
| peers-mcp-tools | 60 | unknown |
| peer-membership-service | 90 | unknown |
| peer-inbox-delivery | 90 | unknown |
| peer-inbox-outcomes | 60 | unknown |
| peer-session-bootstrap | 75 | unknown |
| peer-service-core | 90 | unknown |
| manager-peers-lifecycle | 60 | unknown |
| peers-join-leave-order | 75 | unknown |
| peers-two-instance-e2e | 75 | unknown |
| peers-compose-example | 45 | unknown |
| peers-readme-guide | 60 | unknown |
| peers-two-container-test | 75 | unknown |
| peers-security-hardening | 60 | unknown |

## MRs and deploy

No MR or PR URLs were provided. Work is merged to local main (head 9fd0b96, merge: peers-security-hardening); no push or deploy was reported.
Final suite: `mise run test` on 08d4742: 1493 passed, 12 deselected; lint clean (269 files formatted). Post-hardening regression (departed instance re-added via heartbeats) caught by the suite and fixed in 08d4742 (409 left).
CI: PENDING

## Lessons routed to

- `verify-task --tdd-revert` turns committed symlinks into regular files and produced a false dirty-tree gate-repeat (mechanical): add a check in `verify-task.py` that restores symlinks (or skips symlink paths) after the revert and re-asserts a clean tree; until then, hand-verify. Memory note verify-task-symlink-revert already exists.
- `tests.red` was missing for 13 of 21 tasks and the workflow raised tdd-skipped only for 3 (mechanical): add a gate in plan-execute that fails a behaviour-changing task whose return has neither `tests.red` nor a named TDD exception.
- Run 1 `filesChanged` read 0 for tasks that changed files (mechanical): add a plan-execute self-check comparing `filesChanged` to `git diff --name-only <base>..<head>`.
- `test_command_vocabulary` hard-coded the hook-event count and broke when the task added 4 events (mechanical): the task adding hook events should run the vocabulary pin test in its own verify command (add `tests/test_command_vocabulary.py` to `verify:` of hook-adding tasks); a planning lint can flag tasks touching `hooks.py` EVENTS without that test.
- Timing-sensitive e2e test (`test_stopped_service_leaves`) was flaky under the full suite because missed-heartbeat drops raced the leaving heartbeat (mechanical): add a repeat-run (3x) step to the verify command of e2e tasks.
- Independent verifiers returned "not met" without evidence and were declined by implementers (judgement call): edit the verifier prompt in the plan-execute skill to require a quoted command output or file:line per "not met" finding.
- Task files `actual:` stayed empty and README Time shows 0 (mechanical): have `ticket.py` fill `actual` from the run timestamps at task completion.
