---
title: CI pre-commit gate
status: todo
repo: .
value: "Every push is checked by the same hooks locally and in CI, and the tree is clean under them"
files: [.pre-commit-config.yaml, mise.toml, tests/test_precommit_config.py, tests/fixtures/hooks/record_hook.sh]
verify: "uv run pytest tests/test_precommit_config.py && mise run precommit && mise run lint"
depends_on: []
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

# CI pre-commit gate

## Requirement

Add a pre-commit gate to the repo and make the whole tree pass it.

- `.pre-commit-config.yaml` modelled on hikvision-doorbell: pre-commit-hooks v6.0.0 (trailing-whitespace, end-of-file-fixer, check-yaml with --allow-multiple-documents, check-added-large-files, check-merge-conflict), ruff-pre-commit (`ruff` with `--fix`, `ruff-format`; rev matching the ruff version locked in uv.lock), shellcheck-py hook for the `*.sh` files that exist (tests/fixtures/hooks/record_hook.sh), conventional-pre-commit at the commit-msg stage. No poetry, autoflake, isort, vulture, helm or markdown hooks.
- Local hook `uv-lockfile` (language: system, entry `uv lock --check`, pass_filenames false, files `^(pyproject.toml|uv.lock)$`) keeps the lock in sync.
- A mise task `precommit` in mise.toml that runs `uvx pre-commit run --all-files --show-diff-on-failure` (works locally and in CI with no extra install; pre-commit is not added to project dependencies).
- Run it over the tree and fix everything it flags (whitespace, EOF, ruff, shellcheck findings), in the same task. Exclude `.claude/` worktrees and `docs/plans/` generated files from the hooks only if they are untracked or generated; tracked files are fixed, not excluded.
- A test `tests/test_precommit_config.py` parses `.pre-commit-config.yaml` and asserts: every `rev` is a pinned tag (no `main`/`master`), the hook ids above are present, and `mise.toml` defines a `precommit` task invoking `pre-commit run`.
- `mise run lint` and `mise run test` stay green.

## Acceptance conditions

- AC1: .pre-commit-config.yaml declares the pre-commit-hooks, ruff, shellcheck and conventional-pre-commit repos at pinned revs and the uv lock check local hook [evidence: test:tests/test_precommit_config.py::test_config_hooks_and_pinned_revs]
- AC2: mise.toml defines a precommit task that runs pre-commit over all files [evidence: test:tests/test_precommit_config.py::test_mise_precommit_task]
- AC3: the whole tracked tree passes pre-commit with no modification left behind [evidence: command:mise run precommit]
- AC4: lint and the default test suite stay green [evidence: command:mise run lint && mise run test]

## Files

- `.pre-commit-config.yaml`
- `mise.toml`
- `tests/test_precommit_config.py`
- `tests/fixtures/hooks/record_hook.sh`

## Interfaces

mise task `precommit` (mise.toml); config `.pre-commit-config.yaml`; test module tests/test_precommit_config.py. CI task ci-workflows-and-readme calls `mise run precommit`.

## Expected outcome

`mise run precommit` exits 0 on a clean checkout; the tree contains no whitespace, EOF, ruff or shellcheck findings; the config pins every rev.

## Steps

1. Write tests/test_precommit_config.py (parse YAML with pyyaml, tomllib for mise.toml); run `uv run pytest tests/test_precommit_config.py`; expect failures (no config).
2. Add .pre-commit-config.yaml and the mise `precommit` task; rerun; expect pass.
3. Run `mise run precommit`; fix every flagged file (re-run until it exits 0); `git diff` shows only formatting/lint fixes.
4. Run `mise run lint && mise run test`; expect green.

## Notes

Reference: hikvision-doorbell .pre-commit-config.yaml. Blocker epic (microservice) provides Dockerfile/compose/mise.toml. No .py scratch files. README edits belong to ci-workflows-and-readme.
