---
title: Repo scaffold
status: in-progress
repo: claude-code-wrapper
value: A working uv/mise Python project every later task builds on
files: [pyproject.toml, uv.lock, mise.toml, .gitignore, CLAUDE.md, .claude/workbench.json, src/claude_code_wrapper/__init__.py, tests/test_scaffold.py]
verify: "uv run pytest tests/test_scaffold.py -q && mise run lint"
depends_on: []
owns: [project-scaffold]
uses: []
model: ""
estimate: 45
actual: ""
review: light
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/claude-code-wrapper-microser--repo-scaffold
branch: claude-code-wrapper-microser--repo-scaffold
verified_head: ""
run: ""
provides: []
---

# Repo scaffold

## Requirement

Create the Python project skeleton for `claude-code-wrapper` (package `claude_code_wrapper`, src layout) so every later task only adds modules and tests.

- `pyproject.toml`: name `claude-code-wrapper`, version `0.1.0`, `requires-python = ">=3.12"`, hatchling build with `packages = ["src/claude_code_wrapper"]`. Runtime deps (all declared now, no later task edits pyproject.toml or uv.lock): `fastapi`, `uvicorn[standard]`, `pydantic>=2`, `pyyaml`, `httpx`, `prometheus-client`. `[dependency-groups] dev`: `pytest>=8.3`, `pytest-asyncio`, `ruff`. `[project.scripts]`: `ccw = "claude_code_wrapper.cli:main"` and `ccw-hook = "claude_code_wrapper.hook_relay:main"` (modules arrive in later tasks). `[tool.ruff]` line-length 100, target-version py312, lint select E, F, I, UP, B, SIM. `[tool.pytest.ini_options]` testpaths `["tests"]`, addopts `-q`, `asyncio_mode = "auto"`.
- `uv.lock` generated with `uv lock` and committed.
- `mise.toml`: `[tools] python = "3.12"`, `uv = "latest"`; `[env] _.python.venv = { path = ".venv", create = true }`, `UV_PYTHON_DOWNLOADS = "never"` only if the pinned python is provided by mise; tasks, each with a `description`: `install` (`uv sync`), `test` (`uv run pytest`), `lint` (`uv run ruff check . && uv run ruff format --check .`), `fmt` (`uv run ruff format . && uv run ruff check --fix .`), `run` (`uv run ccw serve --config config.example.yaml`), `compose` (`docker compose up --build`), `compose:down` (`docker compose down`), `docker:build` (`docker build -t claude-code-wrapper:local .`).
- `.gitignore`: `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.ruff_cache/`, `.venv/`, `.DS_Store`, `.env`, `config.yaml`, `docs/plans/facts.md`, `docs/plans/index.json`, `.claude/worktrees/`, `.worktrees/`, `**/.claude/settings.local.json`, `**/.claude/scheduled_tasks.lock`, `**/.claude/scheduled_tasks.json`, `**/.claude/checkpoints/`, `**/.claude/mailbox/`, `**/.claude/agent-registry.json`.
- `CLAUDE.md` for the repo: what the service is (one paragraph), module layout (from design.md Architecture), headings `## Commands` (`mise run install|test|lint|fmt|run|compose`), `## Conventions` (test-first red/green/refactor, always `uv run`, plain ASCII, tests use the fake claude under `tests/fake_claude/`, only the fake-claude task edits `tests/conftest.py`, no service files written into a session workdir), and a pointer to `docs/plans/` for the epic design.
- `.claude/workbench.json`: `{"suites": ["mise run lint", "mise run test"]}`.
- `src/claude_code_wrapper/__init__.py` with `__version__ = "0.1.0"`.
- `tests/test_scaffold.py`: `test_package_version` (imports the package; `__version__` equals the pyproject version read with tomllib) and `test_gitignore_covers_runtime_files` (runs `git check-ignore -q` for `.venv/x`, `docs/plans/facts.md`, `.claude/scheduled_tasks.lock`, `config.yaml`, `.env` and asserts each is ignored).

## Acceptance conditions

- AC1: The package imports and its version matches pyproject.toml [evidence: test:tests/test_scaffold.py::test_package_version]
- AC2: Runtime and machine-local files are git-ignored [evidence: test:tests/test_scaffold.py::test_gitignore_covers_runtime_files]
- AC3: mise run lint exits 0 [evidence: command:mise run lint]
- AC4: mise run test exits 0 [evidence: command:mise run test]
- AC5: uv.lock is in sync with pyproject.toml [evidence: command:uv lock --check]
- AC6: .claude/workbench.json is valid for the workbench plugin [evidence: command:python3 /Users/szymonri/Documents/claude-infra-plugin/scripts/lib/workbench.py config --repo .]
- AC7: CLAUDE.md documents the commands [evidence: doc-section:CLAUDE.md#Commands]
- AC8: Both console scripts are declared in pyproject.toml [evidence: command:grep -q 'ccw-hook = "claude_code_wrapper.hook_relay:main"' pyproject.toml && grep -q 'ccw = "claude_code_wrapper.cli:main"' pyproject.toml]

## Files

- `pyproject.toml`
- `uv.lock`
- `mise.toml`
- `.gitignore`
- `CLAUDE.md`
- `.claude/workbench.json`
- `src/claude_code_wrapper/__init__.py`
- `tests/test_scaffold.py`

## Interfaces

- `claude_code_wrapper.__version__: str`.
- Console scripts `ccw` -> `claude_code_wrapper.cli:main` (built by service-main) and `ccw-hook` -> `claude_code_wrapper.hook_relay:main` (built by hook-relay).
- `mise run test` and `mise run lint` are the repo verify commands every later task uses.

## Expected outcome

`uv sync` creates `.venv`; `mise run test` passes two tests; `mise run lint` is clean; the repo has a CLAUDE.md, workbench.json and a complete .gitignore.

## Steps

- Write `tests/test_scaffold.py` first; run `uv run pytest tests/test_scaffold.py -q` and see it fail (package missing).
- Add pyproject.toml, `src/claude_code_wrapper/__init__.py`, `uv lock`, `uv sync`; rerun: version test passes, gitignore test fails.
- Add .gitignore; rerun: both pass.
- Add mise.toml, CLAUDE.md, .claude/workbench.json; run `mise run lint` and `mise run test` (expect `2 passed`).

## Notes

- The repo has an untracked `.claude/scheduled_tasks.lock`; it must end up ignored, never committed.
- Exemplar shapes: `/Users/szymonri/Documents/magic-trainer/code/magic-cards-model-api/mise.toml` and `pyproject.toml`.
- `ccw`/`ccw-hook` entry points reference modules that do not exist yet; that is expected and harmless until invoked.
