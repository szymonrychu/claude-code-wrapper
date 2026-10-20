---
title: Session manager
status: todo
repo: claude-code-wrapper
value: "Many independent sessions, destroyable and resumable by id"
files: [src/claude_code_wrapper/manager.py, src/claude_code_wrapper/prompts.py, tests/test_manager.py, tests/test_prompts.py]
verify: "uv run pytest tests/test_manager.py tests/test_prompts.py -q && mise run lint"
depends_on: [event-router, home-seed, settings-render, plugins-and-mise, workdir-init]
owns: [session-manager, prompt-templates]
uses: [event-router, home-seed, settings-renderer, plugin-installer, mise-tooling, workdir-init, claude-env, fake-claude, session]
model: opus
estimate: 90
actual: ""
review: full
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Session manager

## Requirement

Build the multi-session manager (D4, D2, D12, D14, D11) in `src/claude_code_wrapper/manager.py` and prompt templates in `src/claude_code_wrapper/prompts.py`.

prompts.py:
- `render_template(templates_cfg, name, variables) -> str`: `{{name}}` placeholders; missing required variable -> `PromptError("missing_variable", name)`; defaults applied; unknown template -> `PromptError("unknown_template")`; unknown supplied variable -> `PromptError("unknown_variable")`. `list_templates(cfg)` -> `[{name, description, variables}]`.
- `resolve_prompt(cfg, *, text=None, template=None, variables=None) -> str | None`: exactly one of text/template, else `PromptError("text_or_template")`.

manager.py, `SessionManager(cfg, *, internal_url, service_env, redactor)`:
- `CreateSessionRequest`: `name`, `workdir` (mounted path or name), `model`, `effort`, `env` (partial `ClaudeEnv`), `prompt`, `template`, `variables`.
- `async create(req) -> snapshot`: active sessions >= `max_sessions` -> `TooManySessions`. Ids per design.md. Steps in this order, each recorded in session.json (`state: creating`, `step`): session dir -> `prepare_workdir` -> `write_gitconfig` -> `run_setup_commands` (marker `setup.done`) -> `tooling.install_workdir` -> `ensure_config_dir`, `seed_claude_json`, `write_instructions`, `copy_config_files` -> `install_plugins` -> `render_settings` -> child env (`build_env` with `cfg.claude.env.merged(req.env)`; `CCW_TOOL_GATE_FAIL=deny` when any blocking onAgentToolCall action has `on_error: deny`; `CCW_GATE_TIMEOUT` = PreToolUse timeout) -> EventLog, TurnStore, Session, SessionRouter -> `session.start()` -> fire `onCreate` (blocking deny -> stop claude, state `stopped`, raise `CreateVetoed(reason)`) -> initial prompt (request prompt/template, else `prompts.initial_prompt`) submitted with `origin="initial"` -> state `ready`. A step failure: state `failed` with `{step, message}` (redacted) in session.json, `error` event, `onError` `{kind: "create_failed", step}`, claude stopped if started, `CreateFailed(step, message)` raised. The redactor gets every secret value of the merged env.
- session.json holds: ids, name, workdir, workdir_mode, model, effort, env override as `redacted()` minus secret fields (non-secret override fields kept for resume), created_at, updated_at, state, step, error, last_activity_at. Never a secret value.
- `get(id)`, `list()`: active sessions plus every `<sessions_root>/*/session.json` not active (state as persisted); `router(id)` and `hook_token_matches(id, token)` (constant-time) for the internal API.
- `async destroy(id, purge=False)`: stop claude (`limits.shutdown_timeout_seconds`), close the router, fire `onDestroy` (awaited, bounded), state `stopped`; `purge=True` additionally deletes the session dir and, for modes empty and clone only, the workdir; a mounted workdir is never deleted.
- `async resume(id, env=None)`: allowed for stopped, dead or failed-after-start sessions whose config dir and transcript exist (else `ResumeUnavailable`); re-runs bootstrap rendering (claude.json, instructions, config files, settings) and plugin verification, not setup commands; spawns with `resume=True`; fires `onResume` (veto -> stopped, `ResumeVetoed`); state ready.
- `async load_persisted()`: sessions persisted as creating/ready/busy become `stopped` (their processes died with the service); with `sessions.resume_on_start` they are resumed.
- Idle reaper task: every 30 s destroys (without purge) sessions idle longer than `idle_timeout_seconds` (0 disables).
- `async shutdown()`: destroys all active sessions concurrently within `shutdown_timeout_seconds`, then drains the dispatcher.

## Acceptance conditions

- AC1: create runs every step in order and returns a ready snapshot [evidence: test:tests/test_manager.py::test_create_runs_steps_in_order_and_is_ready]
- AC2: max_sessions is enforced [evidence: test:tests/test_manager.py::test_max_sessions_enforced]
- AC3: A failing step marks the session failed and fires onError with the step [evidence: test:tests/test_manager.py::test_create_failure_marks_failed_and_fires_on_error]
- AC4: An onCreate veto stops the session and raises CreateVetoed [evidence: test:tests/test_manager.py::test_on_create_veto_destroys]
- AC5: The initial prompt is submitted with origin initial [evidence: test:tests/test_manager.py::test_initial_prompt_submitted]
- AC6: The per-session env override is merged into the child env and secret values are not persisted [evidence: test:tests/test_manager.py::test_env_override_merged_and_not_persisted_secret]
- AC7: destroy fires onDestroy and keeps files [evidence: test:tests/test_manager.py::test_destroy_fires_on_destroy_and_keeps_files]
- AC8: purge removes the session dir but never a mounted workdir [evidence: test:tests/test_manager.py::test_purge_removes_session_dir_not_mounted_workdir]
- AC9: resume relaunches with --resume and fires onResume [evidence: test:tests/test_manager.py::test_resume_uses_resume_flag_and_fires_on_resume]
- AC10: resume without a transcript is rejected [evidence: test:tests/test_manager.py::test_resume_without_transcript_rejected]
- AC11: list includes persisted sessions after a manager reload [evidence: test:tests/test_manager.py::test_list_includes_persisted_sessions_after_reload]
- AC12: The idle reaper destroys idle sessions [evidence: test:tests/test_manager.py::test_idle_reaper_destroys_idle]
- AC13: shutdown stops every session within the bound [evidence: test:tests/test_manager.py::test_shutdown_stops_all]
- AC14: Templates render variables and defaults [evidence: test:tests/test_prompts.py::test_render_variables_and_defaults]
- AC15: A missing required or unknown variable raises PromptError [evidence: test:tests/test_prompts.py::test_missing_and_unknown_variables]
- AC16: Exactly one of text or template is required [evidence: test:tests/test_prompts.py::test_text_or_template]
- AC17: The tool gate fail mode and timeout reach the child env [evidence: test:tests/test_manager.py::test_gate_env_from_hooks]

## Files

- `src/claude_code_wrapper/manager.py`
- `src/claude_code_wrapper/prompts.py`
- `tests/test_manager.py`
- `tests/test_prompts.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
