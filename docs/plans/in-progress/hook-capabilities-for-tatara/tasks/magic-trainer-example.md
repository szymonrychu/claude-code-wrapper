---
title: magic-trainer example
status: todo
repo: .
value: "A tested worked example of magic-trainer's seat-pair completion, review, narration and exit as ccw hooks"
files: [src/claude_code_wrapper/data/examples/magic-trainer.config.yaml, src/claude_code_wrapper/data/examples/magic-trainer/mtg_hooks.py, examples/magic-trainer.config.yaml, README.md, tests/test_example_magic_trainer.py]
verify: "uv run pytest tests/test_example_magic_trainer.py tests/test_examples.py -q && mise run lint"
depends_on: [tatara-example-turn-hooks, tatara-example-lifecycle]
owns: []
uses: [fake-claude-scenarios, hook-result-fields, on-stop-event, destroy-io, exit-policy, agent-attribution, turn-annotations, hook-context-env, ccw-world-fixture]
model: sonnet
estimate: 90
actual: ""
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/hook-capabilities-for-tatara--magic-trainer-example
branch: hook-capabilities-for-tatara--magic-trainer-example
verified_head: ""
run: hook-capabilities-for-tatara
provides: []
acs_for_audit: []
process_gaps: []
---

# magic-trainer example

## Requirement

magic-trainer example (design D18; parity rows #5, #9, #13-#15, #18, #19, #21-#25). Shows how magic-trainer's harness behaviour is expressed with ccw hooks; no mtg code paths in ccw core and no consumer repo changes.

Files: `src/claude_code_wrapper/data/examples/magic-trainer.config.yaml`, `src/claude_code_wrapper/data/examples/magic-trainer/mtg_hooks.py` (python stdlib only, subcommands), symlink `examples/magic-trainer.config.yaml` -> `../src/claude_code_wrapper/data/examples/magic-trainer.config.yaml`. Values from the env use `${VAR:-default}`; actions run `${MTG_PYTHON:-python3} ${MTG_HOOKS:-/opt/mtg/mtg_hooks.py} <subcommand>`. No secrets in files.

Config and subcommands:
- Seat pair completion: `sessions.defer_stop_on_background_tasks: true`; agent teams via `claude.env` (`${AGENT_TEAMS:-}`) and `claude.settings.teammateMode: in-process`.
- `pending-tasks` (blocking onStop; the R1 fallback for teammates missing from background_tasks): reads `data.transcript_path`, finds background work started since the turn's last marker and not finished (background Agent/Task and Bash tool uses, teammates spawned, minus their completion or TaskStop), prints `{"decision": "defer", "annotations": {"pending_tasks": [...]}}` when any, else `{"decision": "allow", "annotations": {"pending_tasks": []}}`.
- `review` (onDestroy, `blocking: true` (required: hook_outputs in the DELETE response collects only blocking onDestroy actions), `timeout_seconds: ${REVIEW_TIMEOUT_SECONDS:-4800}`): reads `data.input` `{prompt, model, effort}`; runs `${CLAUDE_BIN:-claude} -p --setting-sources "" --strict-mcp-config --model <model>` with the prompt on stdin in the session workdir and `CLAUDE_CONFIG_DIR=$CCW_CLAUDE_CONFIG_DIR`; prints `{"output": {"text": <stdout>}}`. No input: no review, `{"output": null}`.
- `narrate` (non-blocking onAgentToolCall and onAgentResponse): appends one line per event, `step tool=<name> agent=<agent_type or main>` or `say <first 80 chars>`, to `${NARRATION_LOG:-/proc/1/fd/1}`.
- `sessions.exit_when_last_destroyed: true`, `sessions.max_restarts: 0`, `sessions.ask_user_question: deny`, the MCP limits in `claude.env`, and `init.setup_commands` for the git credential helper (token file path from env), `safe.directory /guides` and the agent renderer (`${MTG_AGENTS_RENDER:-true}`, the consumer's renderer command).

Test `tests/test_example_magic_trainer.py` against the fake claude via the `world` fixture (the fake's print mode answers the review). README `## Hook examples` (created by tatara-example-turn-hooks; this task and tatara-example-lifecycle both edit the README, hence the dependency) gains a `### magic-trainer` heading with the files and the parity row -> hook mapping.

## Acceptance conditions

- AC1: the magic-trainer example config loads with the stub env and its files ship inside the package [evidence: test:tests/test_example_magic_trainer.py::test_magic_config_loads_and_ships]
- AC2: a seat-pair turn completes only at the Stop with no background tasks [evidence: test:tests/test_example_magic_trainer.py::test_pair_turn_completes_after_background_tasks]
- AC3: the pending-tasks fallback defers from the transcript and annotates pending_tasks [evidence: test:tests/test_example_magic_trainer.py::test_pending_tasks_fallback_defers_from_transcript]
- AC4: the review output from claude -p is returned in the DELETE response [evidence: test:tests/test_example_magic_trainer.py::test_review_output_in_delete_response]
- AC5: without hook_input no review runs and the output is null [evidence: test:tests/test_example_magic_trainer.py::test_review_skipped_without_input]
- AC6: narration lines are written with tool and agent [evidence: test:tests/test_example_magic_trainer.py::test_narration_lines_written]
- AC7: README has the magic-trainer example section [evidence: doc-section:README.md#magic-trainer]

## Files

- `src/claude_code_wrapper/data/examples/magic-trainer.config.yaml`
- `src/claude_code_wrapper/data/examples/magic-trainer/mtg_hooks.py`
- `examples/magic-trainer.config.yaml`
- `README.md`
- `tests/test_example_magic_trainer.py`

## Interfaces

- `src/claude_code_wrapper/data/examples/magic-trainer.config.yaml`, `src/claude_code_wrapper/data/examples/magic-trainer/mtg_hooks.py` subcommands `pending-tasks`, `review`, `narrate`.

## Expected outcome

A reader can see, run and test how magic-trainer's seat-pair completion, the claude -p review, narration and one-match-per-process lifecycle are done with ccw hooks and options.

## Steps

- Write `tests/test_example_magic_trainer.py`; see it fail.
- Write the config and script; add the symlink in `examples/`.
- README `### magic-trainer` under `## Hook examples` (created by tatara-example-turn-hooks).
- `uv run pytest tests/test_example_magic_trainer.py tests/test_examples.py -q && mise run lint`.

## Notes

- Parity rows #5, #9, #13-#15, #18, #19, #21-#25. R1 fallback via the transcript. Review timing per design D7 (after claude stopped, before purge).
- No mtg names in `src/claude_code_wrapper/*.py` (anti-goal).

- 2026-10-21 run hook-capabilities-for-tatara: dependency-failed on tatara-example-turn-hooks, tatara-example-lifecycle
