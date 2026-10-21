---
title: GitHub CI pipeline with Harbor push
blockers: [claude-code-wrapper microservice]
stage: review
owner: Szymon Richert
repos: [.]
created: 2026-10-20
---

# GitHub CI pipeline with Harbor push

## Project brief

What: GitHub Actions pipeline for claude-code-wrapper: pre-commit, unit, integration tests, then a container push to Harbor.
How: Follow hikvision-doorbell (build.yaml, release.yaml, renovate) with mise tasks for every stage; compose-based integration tests against the real image with the fake claude.
Why: Every merge to main yields a tested image in Harbor, with no manual steps.

## Description

Workflows build.yaml (pre-commit -> unit -> integration -> build-push, push only on main) and release.yaml (semver tag retags the sha image as X.Y.Z and latest), renovate.json, a pre-commit config, and a `mise run test:integration` task. Lean path: no design.md; tasks carry the ACs.

Gate operations (orchestrator, not tasks, listed in the repo README CI section): create public repo szymonrychu/claude-code-wrapper; set HARBOR_USERNAME='robot$gitlab' and HARBOR_PASSWORD via gh secret set; history secret scan before the first public push; push main; watch the first pipeline green including the Harbor push.


## Goals

- Pre-commit step precedes unit and integration tests.
- Integration tests run the built container over HTTP.
- Image pushed to harbor.szymonrichert.pl/containers/claude-code-wrapper:<shortsha> on main; X.Y.Z and latest on tag.
- Actions pinned by SHA; minimal permissions.


## Non-goals

- No Docker Hub, PyPI or GHCR publishing.
- No helm chart, daily-release or auto-merge workflows.
- Repo creation, secrets and first push are operator steps.


## Time

Estimate: 195 min | Actual: 0 min | Remaining estimate: 135 min

## Review

Filled when the epic reaches stage `review`: the verdict, the findings acted on, the findings dropped.

Verdict: pass (2026-10-21)

Epic review, github-ci-pipeline-with-harb, 2026-10-21.
- All 3 tasks verified by verify-task.py (ci-pre-commit-gate 735fd7e by the run's verifier; ci-integration-tests 56e1849 and ci-workflows-and-readme 203bc51 hand-verified after verifier deaths, see task notes) and merged into main (b3df7cb, 25cfe5e).
- Two defects surfaced only on GitHub runners and were fixed test-first on main: 3f03c34 test(cli) fake claude on PATH (4 CLI tests needed a real claude; red reproduced locally by stripping PATH), f9cb057 ci chunked regctl push (Cloudflare 413 on layers >100MB; test_push_is_chunked_under_the_cloudflare_body_limit red then green, local buildx OCI + regctl import checked).
- Pipeline evidence: GitHub run 39563069162 on f9cb057: pre-commit, unit, integration, build-push all success; image harbor.szymonrichert.pl/containers/claude-code-wrapper:f9cb057 present (manifest sha256:968b4402...).
- Local: mise run precommit all hooks Passed; mise run lint All checks passed; mise run test 620 passed, 7 deselected (integration, run separately: 7 passed).
- Release workflow not exercised yet (needs a semver tag); covered by test_release_workflow.
