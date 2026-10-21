---
title: Fake claude hook scenarios
status: in-progress
repo: .
value: Lets every new hook capability be tested end to end against the fake claude
files: [tests/fake_claude/fake_claude.py, tests/conftest.py, tests/test_fake_claude.py, tests/test_manager.py]
verify: "uv run pytest tests/test_fake_claude.py tests/test_manager.py -q && mise run lint"
depends_on: []
owns: [fake-claude-scenarios, ccw-world-fixture]
uses: []
model: sonnet
estimate: 75
actual: ""
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/hook-capabilities-for-tatara--fake-claude-hook-scenarios
branch: hook-capabilities-for-tatara--fake-claude-hook-scenarios
verified_head: ""
run: ""
provides: []
---

# Fake claude hook scenarios

## Requirement

Extend the scripted fake claude and the shared test fixtures so later tasks can drive every new hook capability against it (design D5, D9, D16, D18 test support). This task owns parts `fake-claude-scenarios` (`tests/fake_claude/fake_claude.py`) and `ccw-world-fixture` (`tests/conftest.py`); it is the only task that edits `tests/conftest.py` or `tests/fake_claude/`.

Scenario schema additions (each documented in the module docstring's "Scenario schema"):

1. Stop payload fields: every Stop payload carries `background_tasks` and `session_crons` (default `[]`, as claude 2.1.145+ does). A turn may set `"stop": {"background_tasks": [...], "session_crons": [...]}`; the turn's first Stop carries those lists verbatim (entries like `{"id", "type", "status", "description"}`).
2. Background completion with tasks: the existing `{"background": {"after": s, "text": t}}` step accepts an optional `"background_tasks": [...]` that the later background Stop carries (default `[]`), so a scenario can model "the first Stop lists a running task, a later Stop lists none".
3. Stop block: when a Stop hook prints `{"decision": "block", "reason": R}` on stdout (exit 0) or exits 2 (reason = stderr), the fake writes R to the transcript as a user line, runs the turn's `"on_block"` steps (default `[{"text": "continued: <R>"}]`) and fires Stop again with `stop_hook_active: true`. At most 8 consecutive blocks (env `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` overrides); past the cap the turn ends as if allowed.
4. Agent attribution: a `{"tool": ...}` step may carry `"agent": {"id": A, "type": T}`; its PreToolUse and PostToolUse/PostToolUseFailure payloads then include `agent_id` and `agent_type`, and its tool_use and tool_result transcript lines go to that agent's sidechain file (`subagents/agent-<A>.jsonl`, `isSidechain: true`).
5. Crash once: `{"exit_once": code}` exits with `code` only the first time any fake process reaches it (a flag file under `$CLAUDE_CONFIG_DIR`); afterwards the step is a no-op, so a resumed fake survives the same scenario.
6. Print mode: `fake_claude -p [prompt]` / `--print` takes the prompt from the argument or stdin, needs no TTY, picks the first scenario turn whose regex matches, prints the concatenated `text` steps (default `echo: <prompt>`) and exits 0; with `--output-format json` it prints `{"result": <text>}`. Other flags (`--model`, `--setting-sources`, `--strict-mcp-config`, ...) are accepted and logged via `FAKE_CLAUDE_LOG` like every invocation.

Shared fixture:

7. Move the `World` harness (config factory `cfg`, `manager`, loopback hook relay server, callback capture, `session_dir`, `meta`, `events`, `interactive_argvs`) and the `world` fixture from `tests/test_manager.py` into `tests/conftest.py`, unchanged in behaviour, plus two methods: `world.until(pred, timeout=15.0)` (bounded async poll that fails naming what it waited for) and `world.wait_turn(manager, session_id, state="completed")`. `tests/test_manager.py` uses the conftest fixture and drops its local copy; all its tests stay green unchanged.

Existing scenarios and tests behave exactly as before.

## Acceptance conditions

- AC1: Every Stop carries background_tasks and session_crons, and a turn's "stop" lists reach its first Stop [evidence: test:tests/test_fake_claude.py::test_stop_carries_background_tasks]
- AC2: A background step's later Stop carries its own background_tasks [evidence: test:tests/test_fake_claude.py::test_background_stop_carries_its_own_tasks]
- AC3: A Stop hook block (JSON or exit 2) continues the turn with the reason and refires Stop with stop_hook_active true [evidence: test:tests/test_fake_claude.py::test_stop_block_continues_turn_with_reason]
- AC4: Consecutive Stop blocks are capped at 8 and the env var overrides the cap [evidence: test:tests/test_fake_claude.py::test_stop_block_cap]
- AC5: A tool step with "agent" puts agent_id and agent_type in Pre/PostToolUse payloads and writes to the sidechain [evidence: test:tests/test_fake_claude.py::test_tool_step_agent_attribution]
- AC6: exit_once exits only on the first run and is a no-op after a resume [evidence: test:tests/test_fake_claude.py::test_exit_once_survives_resume]
- AC7: Print mode answers from the scenario without a TTY, in text and json output formats [evidence: test:tests/test_fake_claude.py::test_print_mode]
- AC8: The World harness and world fixture live in tests/conftest.py with until and wait_turn [evidence: symbol:tests/conftest.py::World]
- AC9: tests/test_manager.py runs green on the conftest world fixture [evidence: command:uv run pytest tests/test_manager.py -q]

## Files

- `tests/fake_claude/fake_claude.py`
- `tests/conftest.py`
- `tests/test_fake_claude.py`
- `tests/test_manager.py`

## Interfaces

- Part `fake-claude-scenarios`: the scenario schema in the `tests/fake_claude/fake_claude.py` docstring (turn `stop`, background `background_tasks`, `on_block`, tool `agent`, `exit_once`, `-p`/`--print`).
- Part `ccw-world-fixture`: `tests/conftest.py::World` and the `world` fixture; methods `cfg(scenario=None, **sections)`, `manager(cfg, **kw)`, `until(pred, timeout)`, `wait_turn(manager, session_id, state)`, `events(session_id)`, `meta(session_id)`, `session_dir(session_id)`.

## Expected outcome

Every later task can write an end-to-end test with `world` and a scenario dict that exercises background tasks, Stop blocks, subagent tool calls, a one-time crash and print mode; the existing suite is unchanged and green.

## Steps

- Write the new tests in `tests/test_fake_claude.py`; run `uv run pytest tests/test_fake_claude.py -q` and see them fail.
- Implement the scenario additions in the fake; rerun until green.
- Move `World` and `world` into `tests/conftest.py`, drop the copy in `tests/test_manager.py`; `uv run pytest tests/test_manager.py -q` green.
- `uv run pytest tests/test_fake_claude.py tests/test_manager.py -q && mise run lint`.

## Notes

- The fake stays stdlib only and runs under `-I`; keep the existing default behaviour byte for byte (other tests assert on it).
- Claude's real Stop block semantics: `decision: block` plus reason continues the turn, `stop_hook_active` is true on re-entry, cap 8 (`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`, claude 2.1.143).
