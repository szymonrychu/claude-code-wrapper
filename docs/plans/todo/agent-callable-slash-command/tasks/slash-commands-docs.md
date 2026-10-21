---
title: Slash commands docs
status: todo
repo: .
value: "Operators can find how to enable, call and limit slash commands, with working examples for tatara and magic-trainer"
files: [README.md, config.example.yaml, src/claude_code_wrapper/data/examples/workbench.config.yaml, src/claude_code_wrapper/data/examples/tatara.config.yaml, src/claude_code_wrapper/data/examples/magic-trainer.config.yaml, tests/test_slash_commands_docs.py]
verify: "uv run pytest tests/test_slash_commands_docs.py tests/test_examples.py tests/test_example_tatara.py tests/test_example_tatara_lifecycle.py tests/test_example_magic_trainer.py tests/test_readme_api.py tests/test_readme_config.py -q && mise run lint"
depends_on: [ccw-mcp-injection-and-e2e, commands-public-endpoint, idle-auto-compact]
owns: []
uses: [auto-compact-config, commands-config]
model: sonnet
estimate: 45
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Slash commands docs

## Requirement

Docs and examples for slash commands and auto-compact (design D9). README sections owned by earlier tasks (config rows, routes, events, hooks) stay where they are; this task adds the narrative section and the examples.

- README `## Slash commands`, placed after `## Hook examples`, with `### The ccw command tool` (tool name, input schema, per-command params, success text, errors, main agent only, claude.json and allow-rule injection with env references), `### Commands endpoint` (the three routes, link to their API sections), `### Lifecycle` (queued during a turn -> runs at Stop -> same-turn resume with the continue prompt; idle sessions run at once and are busy meanwhile; onStop block and background defer keep commands queued; interrupt, crash and turn_max_seconds), `### Configuration` (a full `sessions.commands` example), `### Idle auto-compact` (`sessions.auto_compact`, once per idle gap, min_context_tokens, reaper interaction, Claude Code's own idle compaction left as is and reported as `compacted`), `### Limits` (session-replacing, dialog and skill commands are not supported; no automatic `--force`; `--force` only with allow_force; the completion signals ccw relies on).
- `config.example.yaml`: commented `sessions.commands` and `sessions.auto_compact` blocks with every key at its default and a one-line comment each.
- `src/claude_code_wrapper/data/examples/workbench.config.yaml`: enables `sessions.commands` (agent: compact, reload-plugins; client: all five) and `sessions.auto_compact` (idle_seconds 1800, one-line instructions).
- `src/claude_code_wrapper/data/examples/tatara.config.yaml` and `src/claude_code_wrapper/data/examples/magic-trainer.config.yaml`: an `auto_compact` block (idle_seconds 1800, instructions suited to each project) and `commands.agent: [compact, reload-plugins]` under `sessions`. `examples/*.config.yaml` are symlinks to these files, so they are not edited.
- README `## Hook examples`: the tatara and magic-trainer bullets mention the auto-compact block.
- Every example config still loads (tests/test_examples.py) and the existing example tests (tests/test_example_tatara.py, tests/test_example_tatara_lifecycle.py, tests/test_example_magic_trainer.py) stay green without being edited.

Interfaces: none.

## Acceptance conditions

- AC1: README has the Slash commands section [evidence: doc-section:README.md#Slash commands]
- AC2: README has the six Slash commands subsections in order [evidence: test:tests/test_slash_commands_docs.py::test_readme_slash_commands_subsections]
- AC3: config.example.yaml carries every sessions.commands and sessions.auto_compact key at its model default [evidence: test:tests/test_slash_commands_docs.py::test_config_example_documents_command_keys]
- AC4: the workbench example enables commands and auto_compact and loads [evidence: test:tests/test_slash_commands_docs.py::test_workbench_example_enables_commands]
- AC5: the tatara and magic-trainer examples carry an auto_compact block and agent commands and load [evidence: test:tests/test_slash_commands_docs.py::test_project_examples_auto_compact]
- AC6: the README hook examples mention auto-compact for both projects [evidence: test:tests/test_slash_commands_docs.py::test_hook_examples_mention_auto_compact]
- AC7: all example and readme tests stay green [evidence: command:uv run pytest tests/test_examples.py tests/test_example_tatara.py tests/test_example_tatara_lifecycle.py tests/test_example_magic_trainer.py tests/test_readme_api.py tests/test_readme_config.py -q]

## Files

- `README.md`
- `config.example.yaml`
- `src/claude_code_wrapper/data/examples/workbench.config.yaml`
- `src/claude_code_wrapper/data/examples/tatara.config.yaml`
- `src/claude_code_wrapper/data/examples/magic-trainer.config.yaml`
- `tests/test_slash_commands_docs.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
