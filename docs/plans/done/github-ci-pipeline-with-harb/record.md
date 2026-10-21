# GitHub CI pipeline with Harbor push record

## Summary

Asked: GitHub Actions pipeline for claude-code-wrapper (pre-commit -> unit -> integration -> build/push to Harbor only, release by tag, renovate, README CI section). Shipped: all 3 tasks merged to main (b3df7cb, 25cfe5e), plus two test-first fixes on main (3f03c34, f9cb057) for defects seen only on GitHub runners. First green pipeline: GitHub Actions run 39563069162 on f9cb057 (pre-commit, unit, integration, build-push success; image harbor.szymonrichert.pl/containers/claude-code-wrapper:f9cb057 present). Verdict: pass. Only 1 of 3 tasks was verified by the run's own verifier; the other 2 were hand-verified by the orchestrator after verifier failures. The release workflow is not yet exercised (needs a semver tag).

## Planned vs built

Run wf_ae9f61ef-fa8 (run8-return.json) and run wf_ce8353ff-c37 (ci-workflows-and-readme only). Review rounds and continuations for wf_ce8353ff-c37: unknown (only its summary in task notes).

| Task | Status | Review rounds | Continuations | Files | Verifier head | Verification tail |
|---|---|---|---|---|---|---|
| ci-pre-commit-gate | done | 3 | unknown | .pre-commit-config.yaml, mise.toml, tests/test_precommit_config.py, tests/fixtures/hooks/record_hook.sh (run return filesChanged: tests/test_precommit_config.py only, docstring edit in last round) | 735fd7e0e8a3abb6d9706e498b6a0888bfdecd7d | verify-task passed: 6 passed (precommit config), mise run precommit all hooks Passed, lint All checks passed, 609 passed. Red evidence: the return's `tests.added` is empty and `wroteTestFirst` false (docstring-only exception for the last round); no implementer `tests.red` run is quoted for the earlier rounds - unknown |
| ci-integration-tests | done | 1 (run8: stopped blocked) | unknown | mise.toml, pyproject.toml, scripts/integration.sh, docker-compose.ci.yml, tests/integration/{config.ci.yaml,conftest.py,test_container_http.py,claude-shim.sh,scenario.json,__init__.py} | unverified (run verifier died on `--prev-head unknown`; no `verification.head`). Hand-verified by orchestrator on 56e1849 against base 735fd7e | Hand run: verify-task passed, tdd failedOnBase/passedOnHead (a tdd-revert result, not red evidence); `mise run test:integration` 7 passed. Implementer `tests.red`: none recorded in the run8 return (`wroteTestFirst` false) - unknown |
| ci-workflows-and-readme | done | 0 in run8 (dependency-failed); later run wf_ce8353ff-c37 escalated verify-agent-died | unknown | .github/workflows/build.yaml, .github/workflows/release.yaml, renovate.json, tests/test_ci_workflows.py, README.md | unverified (no `verification.head` from either run). Hand-verified by orchestrator on 203bc51 | Hand run: verify-task --base main passed, tdd failedOnBase/passedOnHead, AC6-7 met, `mise run test` green. Implementer `tests.red`: not in the run8 return (task not run there) - unknown |

Files contradicting summaries: run8 return lists ci-integration-tests `filesChanged: []`, `clean: false`, `stopped: blocked`; `git diff --stat 856d1d6..f9cb057` shows its files merged (b3df7cb). The files win: built and merged, but not verified by the run.

Approvals (ledger `approvals.json`, all `approved`; run8 return status shown, then final):

| Id | Text | Decisions | Tasks | Status |
|---|---|---|---|---|
| A | Pre-commit gate (hikvision-modelled config, mise precommit task) precedes the tests, and the tree passes it. | none | ci-pre-commit-gate | delivered (run verifier, 735fd7e) |
| B | Integration tests against the real container (compose, fake claude) run by mise run test:integration. | none | ci-integration-tests | delivered, hand-verified (run8 return said blocked: Docker Desktop wedged; cleared by orchestrator ruling 1) |
| C | GitHub pipeline pre-commit -> unit -> integration -> build/push to Harbor only (no Docker Hub, no PyPI), release by tag, renovate, README CI section. | none | ci-workflows-and-readme | delivered, hand-verified (run8 return said escalated: dependency-failed; later verify-agent-died, ruling 2). Release-by-tag part not exercised live |

## Deviations and rulings

1. ci-integration-tests blocked/escalated.
   - What: Docker Desktop wedged locally and the verifier died on `--prev-head unknown`. The orchestrator restarted Docker Desktop and ran `mise run test:integration` (7 passed). It hand-ran `verify-task.py --base 735fd7e`: passed, tdd failedOnBase/passedOnHead. AC4's evidence command was wrong (`-q` on top of addopts `-q` prints per-file counts, 0 `::` lines); corrected to `-m integration`, 7 collected.
   - Why: only way to get a verdict when the run's verifier produced none.
   - Cost if wrong: the integration task would carry an unverified pass. Mitigated by the green GitHub integration job on f9cb057.
2. ci-workflows-and-readme escalated verify-agent-died.
   - What: hand-verified at 203bc51 (passed, tdd ok). AC1-5 evidence refs renamed to the real test names (companions in task notes: AC1 + test_push_and_login_gated_on_main_push, AC3 + test_image_refs_use_harbor_containers, AC5 + test_renovate_pins_digests). Pinned action SHAs checked against upstream tags (checkout v7.0.0, mise-action v4.3.0, cache v6.1.0).
   - Why: the planned test names did not match the implementer's.
   - Cost if wrong: ACs would point at tests that do not exist; a mis-pinned SHA would break or compromise CI.
3. Two post-merge defects fixed test-first on main by the orchestrator.
   - 3f03c34 `test(cli)`: 4 CLI tests needed a real claude on PATH (GitHub unit run 39561052934 failed); fixed by putting the fake claude on PATH; red reproduced locally by stripping PATH.
   - f9cb057 `ci`: build-push failed with Cloudflare 413 on layers >100MB (run 39561709137); fixed with a chunked regctl push; `test_push_is_chunked_under_the_cloudflare_body_limit` red then green, local buildx OCI + regctl import checked.
   - Why: only visible on GitHub runners, not in the task verifiers.
   - Cost if wrong: red main pipeline and no image in Harbor.
4. Workflow tooling bug (`verify-task --prev-head unknown`): known plugin issue, already in memory; not fixed here.
   - Cost: repeated verifier deaths on dependent or re-run tasks, forcing hand verification.

Declined in run8 (accepted as correct): shim uses /usr/local/bin/python instead of /app/.venv/bin/python (Dockerfile installs into system python, no .venv); integration marker applied in tests/integration/conftest.py rather than globally; independent AC1/AC2 "not met" findings on ci-pre-commit-gate (config has all repos at pinned revs plus uv-lockfile hook, tests passed). `forwardMerged`: none.

## Duplicates reconciled

none

## Discovered and blocked

- Release workflow (`release.yaml`) never run live; covered only by `test_release_workflow`. Cleared by: the user pushing a vX.Y.Z tag on a commit already built on main.
- cannotVerify (run8): conventional-pre-commit commit-msg hook behaviour (only config shape checked); CI execution of the precommit task (since confirmed by green run 39563069162 for the CI job; commit-msg hook still unchecked); SSE events integration test only checks status and content-type, not event content (turn result checked via transcript). Clearable by: a follow-up test task, owner the user.
- Docker bind-mount start hanging under ~/Documents on the dev machine (implementer's 7/7 was from a /tmp copy); cleared after Docker Desktop restart, root cause unknown.
- ARC runner reachability to Harbor: infra change helmfile MR !1541 (hostAliases pinning harbor.szymonrichert.pl to 192.168.1.25), merged and applied by the user.
- Run8 return showed `escalated`/`blocked`/`undelivered` for ci-integration-tests and ci-workflows-and-readme; all cleared by rulings 1 and 2.

## Research that changed the plan

none

## Cost

Agents by role, continuations, audits, wall-clock: unknown (audit was `off`; run8 `rounds: 1`, triage calls 2, 4 in / 256 out tokens, verifyRuns 3 / 2 / 0).

| Task | Estimate (min) | Actual (min) |
|---|---|---|
| ci-pre-commit-gate | 60 | unknown (task file `actual` empty) |
| ci-integration-tests | 75 | unknown |
| ci-workflows-and-readme | 60 | unknown |

README Time: Estimate 195 min, Actual 0 min, Remaining estimate 135 min (not updated; unreliable).

## MRs and deploy

- Repo: https://github.com/szymonrychu/claude-code-wrapper (public, created this epic). Secrets HARBOR_USERNAME (robot$gitlab) and HARBOR_PASSWORD set from 1Password.
- Work landed directly on main (no PR). Pipeline history: run 39561052934 (unit failed: CLI tests needed real claude), run 39561709137 (build-push failed: Cloudflare 413), run 39563069162 on f9cb057 (all four jobs success).
- Image harbor.szymonrichert.pl/containers/claude-code-wrapper:f9cb057 present (manifest sha256:968b4402...).
- Related: helmfile MR https://gitlab.com/szymonrychu/helmfile/-/merge_requests/1541, merged and applied by the user.
- Local at close: `mise run precommit` all hooks Passed; `mise run lint` All checks passed; `mise run test` 620 passed, 7 deselected; `mise run test:integration` 7 passed.
- Release tag deploy: not exercised.

## Lessons routed to

- Verifier death on `--prev-head unknown` (mechanical): add a test in verify-task.py that rejects or resolves a non-ref `--prev-head` (treat `unknown` as "no previous head") instead of calling `git diff unknown HEAD`. Plugin issue already in memory; not fixed here.
- Tests needing a real claude passed locally but failed on runners (mechanical): add a test/fixture that runs the default suite with PATH stripped of any real claude (what 3f03c34 reproduced by hand), in the unit job or a conftest guard.
- Registry body limit (Cloudflare 413) only seen on a real push (mechanical): `test_push_is_chunked_under_the_cloudflare_body_limit` now pins chunked push in test_ci_workflows.py; no further check.
- AC evidence commands with `-q` stacked on addopts `-q` produce wrong counts (mechanical): add a check in the task-lint step that rejects `--collect-only -q` ACs when addopts already has `-q`.
- AC evidence refs naming tests the implementer does not write (judgement): edit the task-writing guidance in start-development to say test names in AC refs are provisional and get reconciled at verify time.
- First-push CI defects only visible on runners (judgement): edit start-development gate-operations guidance to require watching the first pipeline and budgeting fix-forward commits on main.
