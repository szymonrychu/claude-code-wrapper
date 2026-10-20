---
title: CI workflows and README
status: todo
repo: .
value: "Merges to main build, test and push the image to Harbor; tags release it"
files: [.github/workflows/build.yaml, .github/workflows/release.yaml, renovate.json, tests/test_ci_workflows.py, README.md]
verify: "uv run pytest tests/test_ci_workflows.py && mise run lint"
depends_on: [ci-pre-commit-gate, ci-integration-tests]
owns: []
uses: []
model: ""
estimate: 60
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# CI workflows and README

## Requirement

Add the GitHub Actions pipeline, release flow, renovate config, a test pinning them, and the README CI section.

- `.github/workflows/build.yaml` (`name: main`): on push to main, pull_request to main, workflow_dispatch; `permissions: contents: read`; `concurrency: group: ${{ github.workflow }}-${{ github.ref }}` with cancel-in-progress only for pull_request events; env `HARBOR_URL: harbor.szymonrichert.pl`, `CONTAINER_NAME: claude-code-wrapper`. Jobs, strictly chained with `needs`: `pre-commit` (checkout, jdx/mise-action, `mise install`, `mise run install`, pre-commit cache on ~/.cache/pre-commit keyed by hashFiles of .pre-commit-config.yaml, `mise run precommit`) -> `unit` (`mise run lint`, `mise run test`) -> `integration` (`mise run test:integration`, timeout-minutes 20) -> `build-push` (checkout; shortsha = first 7 chars of github.sha; `docker build -t $HARBOR_URL/containers/$CONTAINER_NAME:<shortsha> .` on every run; login and push only when `github.event_name == 'push'` and ref is main, using `echo "$HARBOR_PASSWORD" | docker login "$HARBOR_URL" -u "$HARBOR_USERNAME" --password-stdin` with the username and password from `secrets.HARBOR_USERNAME` / `secrets.HARBOR_PASSWORD`). Each job has timeout-minutes. No Docker Hub, no PyPI, no GHCR, no chart.
- `.github/workflows/release.yaml`: on push of tags `v[0-9]+.[0-9]+.[0-9]+`; `permissions: contents: read`; one job: derive version = tag without leading `v`, shortsha = `git rev-parse --short=7 ${GITHUB_SHA}`, login, `docker pull :<shortsha>`, tag and push `:<version>` and `:latest`; fail with a clear message when the :<shortsha> image does not exist (the tagged commit must have passed main first). Harbor tag immutability: never re-push an existing tag.
- Every `uses:` pinned to a full 40-char SHA with a `# vN` comment. Use: actions/checkout `9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0` # v7; actions/cache `55cc8345863c7cc4c66a329aec7e433d2d1c52a9` # v6; jdx/mise-action `c2a87611a18de5b3828c5652fe268e992400cb5c` # v4; renovate flow not needed as a workflow.
- `renovate.json` at the repo root: `extends: ["config:recommended"]`, `pinDigests: true`, github-actions and dockerfile and uv managers on (defaults), weekly schedule not required; no self-hosted renovate workflow and no auto-merge workflow.
- `tests/test_ci_workflows.py` parses both workflows with yaml (note: the `on` key loads as boolean True under YAML 1.1; handle it) and asserts: job order via `needs` pre-commit -> unit -> integration -> build-push; push/login steps in build-push are gated on `github.event_name == 'push'` and main; every `uses:` is a 40-hex SHA with a version comment; image refs use `harbor.szymonrichert.pl/containers/claude-code-wrapper`; permissions are `contents: read`; no occurrence of docker.io, hub.docker.com, pypi, twine or ghcr.io anywhere in `.github/`; release.yaml triggers only on semver tags and pushes both the version tag and `latest`; renovate.json has `pinDigests` true.
- README.md gets a `## CI` section (the README is edited by this task only for this section; read it first and keep the rest): status badge for build.yaml, the pipeline description (job chain, push gating, PRs build only), the Harbor image path and tag scheme (`<shortsha>` per main commit, `X.Y.Z` and `latest` on release), how to release (`git tag vX.Y.Z && git push origin vX.Y.Z`, must be a commit already built on main), required secrets `HARBOR_USERNAME` (`robot$gitlab`) and `HARBOR_PASSWORD`, how to run each stage locally (`mise run precommit`, `mise run lint`, `mise run test`, `mise run test:integration`), and a `### Bootstrap (operator steps)` list: create the public repo szymonrychu/claude-code-wrapper; `gh secret set HARBOR_USERNAME --body 'robot$gitlab' -R szymonrychu/claude-code-wrapper` (single quotes, the `$` is literal) and `gh secret set HARBOR_PASSWORD -R ...`; run a history secret scan before the first public push; push main; watch the first pipeline green including the Harbor push.

## Acceptance conditions

- AC1: build.yaml chains pre-commit, unit, integration and build-push via needs and gates login/push on a push to main [evidence: test:tests/test_ci_workflows.py::test_build_job_order_and_push_gating]
- AC2: every action in every workflow is pinned to a 40-char SHA with a version comment [evidence: test:tests/test_ci_workflows.py::test_actions_pinned_by_sha]
- AC3: images go only to harbor.szymonrichert.pl/containers/claude-code-wrapper, with no Docker Hub, PyPI or GHCR reference [evidence: test:tests/test_ci_workflows.py::test_harbor_only]
- AC4: release.yaml triggers on semver tags and retags the sha image as X.Y.Z and latest [evidence: test:tests/test_ci_workflows.py::test_release_retags_version_and_latest]
- AC5: workflow permissions are contents read and renovate.json pins digests [evidence: test:tests/test_ci_workflows.py::test_permissions_and_renovate]
- AC6: README documents the pipeline, secrets, release and bootstrap steps [evidence: doc-section:README.md#CI]
- AC7: lint and the default suite stay green [evidence: command:mise run lint && mise run test]

## Files

- `.github/workflows/build.yaml`
- `.github/workflows/release.yaml`
- `renovate.json`
- `tests/test_ci_workflows.py`
- `README.md`

## Interfaces

Workflows `main` (build.yaml) and `release` (release.yaml); secrets HARBOR_USERNAME, HARBOR_PASSWORD; image ref harbor.szymonrichert.pl/containers/claude-code-wrapper:<tag>; calls `mise run precommit`, `lint`, `test`, `test:integration`.

## Expected outcome

Pushing to main runs pre-commit, unit, integration, then pushes :<shortsha> to Harbor; PRs run the same chain and build without pushing; a vX.Y.Z tag retags that image as X.Y.Z and latest. README documents all of it.

## Steps

1. Write tests/test_ci_workflows.py per the AC names; run it; expect failures (no workflows).
2. Add build.yaml, release.yaml, renovate.json; rerun; expect pass.
3. Add the README `## CI` section; verify with `grep -n '^## CI' README.md`.
4. Run `uv run pytest tests/test_ci_workflows.py && mise run lint && mise run test`; expect green.

## Notes

Harbor `containers` project has tag immutability: push :<shortsha> once, only on push to main; a rerun of a main build may 412, so treat it as fatal-free only by gating, never by re-pushing. GITHUB_TOKEN tag pushes do not trigger workflows (irrelevant: tags are pushed by the user). Username `robot$gitlab` must be passed through secrets, never an unquoted shell. HARBOR_URL is a bare host with no scheme. Reference: hikvision-doorbell build.yaml/release.yaml; mise steps from magic-cards-model ci.yml (mise install, mise run install).
