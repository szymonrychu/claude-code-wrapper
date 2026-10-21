---
title: GitHub CI pipeline with Harbor push
blockers: [claude-code-wrapper microservice]
stage: build
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

Estimate: 195 min | Actual: 0 min | Remaining estimate: 195 min

## Review

Filled when the epic reaches stage `review`: the verdict, the findings acted on, the findings dropped.
