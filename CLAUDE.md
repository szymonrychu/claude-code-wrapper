# claude-code-wrapper

A standalone Python HTTP microservice that runs N long-lived interactive Claude Code sessions,
each under a PTY in its own workdir. It exposes a turns API and an SSE event stream, is set up
from `config.yaml` (init, prompts, typed Claude Code env vars, mise tooling, plugin pre-install),
runs user-defined lifecycle hooks (onCreate, onTurn, onAgentResponse, onAgentToolCall,
onDestroy and more), and ships as a container with docker-compose volumes for workdirs and
past sessions.

## Layout

Package `src/claude_code_wrapper/`: `claude_env.py`, `redact.py`, `config.py`,
`interpolation.py`, `ring.py`, `pty_proc.py`, `bootstrap/{claude_json,instructions,settings}.py`,
`hook_relay.py`, `hooks.py`, `events.py`, `transcript.py`, `workdir.py`, `plugins.py`,
`tooling.py`, `session.py`, `turns.py`, `router.py`, `prompts.py`, `manager.py`,
`internal_api.py`, `api/{app,auth,errors,sessions,turns,events}.py`, `metrics.py`,
`service.py`, `cli.py`, `__main__.py`. Tests live in `tests/`; the fake claude is
`tests/fake_claude/fake_claude.py`.

## Commands

- `mise run install` - uv sync
- `mise run test` - uv run pytest
- `mise run lint` - ruff check and format check
- `mise run fmt` - ruff format and autofix
- `mise run run` - serve with config.example.yaml
- `mise run compose` - docker compose up --build

## Conventions

- Test-first: red, green, refactor. Watch the test fail before implementing.
- Always `uv run`, never bare python or pytest.
- Plain ASCII in code, docs and commit messages.
- Tests use the fake claude under `tests/fake_claude/`, never a real claude.
- Only the fake-claude task edits `tests/conftest.py`.
- Never write service files into a session workdir.

## Design

The epic design and plan live in `docs/plans/` (see `docs/plans/in-progress/`).
