---
title: Fake claude peer harness
status: todo
repo: .
value: Tests can script peer replies and run two real ccw services in one process
files: [tests/fake_claude/fake_claude.py, tests/conftest.py, tests/test_fake_claude_peers.py]
verify: "uv run pytest tests/test_fake_claude_peers.py tests/test_fake_claude.py tests/test_fake_claude_mcp.py -q && mise run lint"
depends_on: []
owns: [fake-match-substitution, ccw-service-fixture]
uses: []
model: sonnet
estimate: 60
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Fake claude peer harness

## Requirement

Test harness for peer messaging (design D12). This is the fake-claude task: the only task of the epic that edits tests/conftest.py.

- tests/fake_claude/fake_claude.py: when a scenario turn's `match` regex matched the prompt, every `{{name}}` in the string values of that turn's steps is replaced with the named group `name` of the match before the step runs. It applies to `text` steps and, recursively through dicts and lists, to `mcp.arguments`. A `{{name}}` with no such group is left as is. A turn without named groups behaves exactly as today. The module docstring's scenario schema documents the substitution.
- tests/conftest.py: a `ccw_service` fixture, an async factory `await ccw_service(cfg) -> RunningService` that runs `claude_code_wrapper.service.run_service(cfg, ready=..., stop=..., handle=...)` as a task, waits for `ready` (bounded, 15 s; a task that ended early re-raises its error), and returns an object with `url` (`http://127.0.0.1:<public port>`), `public_port`, `internal_port` and `async stop()`. Teardown stops every started service and awaits its task.
- tests/conftest.py: `World.service_cfg(name, scenario=None, **sections) -> Config` builds on `World.cfg` and sets `server.host` 127.0.0.1, `server.port` 0, `server.internal_port` 0, `server.auth_token` `tok-<name>-0123456789`, and gives the service its own `paths.workdir_root` and `paths.sessions_root` under `<tmp>/<name>/`, so two services in one test never share session state. Passed sections still override, as in `World.cfg`.
- tests/test_fake_claude_peers.py covers both: a turn whose `match` has named groups gets them substituted into a text step and into nested `mcp.arguments` (proved through `fake-mcp.jsonl` requests or the transcript); a missing group stays literal; two `ccw_service` instances from `service_cfg("a")` and `service_cfg("b")` both answer `GET /healthz` on different ports and use different sessions roots.

Interfaces: `ccw_service` fixture and `World.service_cfg` in tests/conftest.py; the `{{name}}` scenario substitution in tests/fake_claude/fake_claude.py.

Notes: `run_service` renders the global mise config; `service_cfg` must keep every path under tmp_path (set the mise paths the way existing service tests do so nothing is written outside tmp). The existing fake MCP behaviour (headers, env expansion, fake-mcp.jsonl) is unchanged.

## Acceptance conditions

- AC1: named groups of the matched turn regex are substituted into text steps and nested mcp arguments [evidence: test:tests/test_fake_claude_peers.py::test_match_groups_substituted]
- AC2: a placeholder without a matching group stays literal and a turn without groups is unchanged [evidence: test:tests/test_fake_claude_peers.py::test_missing_group_left_literal]
- AC3: two ccw_service instances from service_cfg run on different ports with separate sessions roots [evidence: test:tests/test_fake_claude_peers.py::test_two_services_isolated]
- AC4: the existing fake claude and fake MCP suites stay green [evidence: command:uv run pytest tests/test_fake_claude.py tests/test_fake_claude_mcp.py tests/test_fake_claude_commands.py tests/test_service.py -q]

## Files

- `tests/fake_claude/fake_claude.py`
- `tests/conftest.py`
- `tests/test_fake_claude_peers.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
