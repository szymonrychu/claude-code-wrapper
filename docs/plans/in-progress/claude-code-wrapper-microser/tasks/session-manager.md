---
title: Session manager
status: in-progress
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
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/claude-code-wrapper-microser--session-manager
branch: claude-code-wrapper-microser--session-manager
verified_head: ""
run: claude-code-wrapper-microser
provides: []
acs_for_audit: []
process_gaps: []
---

# Session manager

## Requirement

Build the multi-session manager (D4, D2, D12, D14, D11) in `src/claude_code_wrapper/manager.py` and prompt templates in `src/claude_code_wrapper/prompts.py`.

prompts.py:
- `render_template(templates_cfg, name, variables) -> str`: `{{name}}` placeholders; missing required variable -> `PromptError("missing_variable", name)`; defaults applied; unknown template -> `PromptError("unknown_template")`; unknown supplied variable -> `PromptError("unknown_variable")`. `list_templates(cfg)` -> `[{name, description, variables}]`.
- `resolve_prompt(cfg, *, text=None, template=None, variables=None) -> str | None`: exactly one of text/template, else `PromptError("text_or_template")`.

manager.py, `SessionManager(cfg, *, internal_url, service_env, redactor)`:
- `CreateSessionRequest`: `name`, `workdir` (mounted path or name), `model`, `effort`, `env` (partial `ClaudeEnv`), `prompt`, `template`, `variables`.
- `async create(req) -> snapshot`: active sessions >= `max_sessions` -> `TooManySessions`. Ids per design.md. Steps in this order, each recorded in session.json (`state: creating`, `step`): session dir -> `prepare_workdir` -> `write_gitconfig` -> `run_setup_commands` (marker `setup.done`) -> `tooling.install_workdir` -> `ensure_config_dir`, `seed_claude_json`, `write_instructions`, `copy_config_files` -> `install_plugins` -> `render_settings` -> child env (`build_env` with `cfg.claude.env.merged(req.env)`; `CCW_TOOL_GATE_FAIL=deny` when any blocking onAgentToolCall action has `on_error: deny`; `CCW_GATE_TIMEOUT` = `relay_timeouts(cfg).attempt_timeout` imported from `bootstrap/settings.py`, never recomputed here) -> EventLog, TurnStore, Session, SessionRouter -> `session.start()` -> fire `onCreate` (blocking deny -> stop claude, state `stopped`, raise `CreateVetoed(reason)`) -> initial prompt (request prompt/template, else `prompts.initial_prompt`) submitted with `origin="initial"` -> state `ready`. A step failure: state `failed` with `{step, message}` (redacted) in session.json, `error` event, `onError` `{kind: "create_failed", step}`, claude stopped if started, `CreateFailed(step, message)` raised. The redactor gets every secret value of the merged env.
- session.json holds: ids, name, workdir, workdir_mode, model, effort, env override as `redacted()` minus secret fields (non-secret override fields kept for resume), created_at, updated_at, state, step, error, last_activity_at. Never a secret value.
- `get(id)`, `list()`: active sessions plus every `<sessions_root>/*/session.json` not active (state as persisted); `router(id)` and `hook_token_matches(id, token)` (constant-time) for the internal API.
- `async destroy(id, purge=False)`: stop claude (`limits.shutdown_timeout_seconds`), close the router, fire `onDestroy` (awaited, bounded), state `stopped`; `purge=True` additionally deletes the session dir and, for modes empty and clone only, the workdir; a mounted workdir is never deleted.
- `async resume(id, env=None)`: allowed for stopped, dead or failed-after-start sessions whose config dir and transcript exist (else `ResumeUnavailable`); re-runs bootstrap rendering (claude.json, instructions, config files, settings) and plugin verification, not setup commands; spawns with `resume=True`; fires `onResume` (veto -> stopped, `ResumeVetoed`); state ready.
- `async load_persisted()`: sessions persisted as creating/ready/busy become `stopped` (their processes died with the service); with `sessions.resume_on_start` they are resumed.
- Idle reaper task: every 30 s destroys (without purge) sessions idle longer than `idle_timeout_seconds` (0 disables).
- `async shutdown()`: destroys all active sessions concurrently within `shutdown_timeout_seconds`, then drains the dispatcher.

Settled by the builder check (binding):
- `started: bool` attribute, set by service-main only after config load, global mise install, `load_persisted()` and both listeners bound (readyz uses it).
- `open_event_log(id)` and `open_turns(id)`: the live router's EventLog/TurnStore for active sessions, else read-only instances opened from the session dir (history, turns and SSE work on stopped sessions from disk); unknown id -> `SessionNotFound`.
- Snapshot and `list(state=...)` use exactly `creating, ready, busy, stopped, failed, dead`.
- `create` returns once claude is ready and onCreate passed; it does NOT wait for the initial turn, which is submitted in a tracked background task. A veto (or rejection) of the initial turn emits an `error` event `{kind: "initial_turn_rejected", reason}` and the session stays ready.
- Gate env comes from `relay_timeouts` only (see above).
- Tests deliver `FAKE_CLAUDE_*` via `claude.env.extra` or `claude.passthrough_env`.
- The `metrics=None` constructor kwarg is added by the metrics task.

Settled at the gate (D19-D24, binding):
- `CreateSessionRequest.callbackUrl` (camelCase in the API) is the session default callback, persisted in session.json and used by the router when a turn has none.
- `destroy(id, purge=False)`: files are kept unless purge is requested (D19).
- Secret env override fields are never written to session.json; `resume(id, env)` applies the env given in the resume request, so secret overrides must be resent there (D24).

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

- AC18: Stopped sessions expose history and turns read from disk [evidence: test:tests/test_manager.py::test_open_event_log_and_turns_for_stopped_session]
- AC19: create returns before the initial turn completes [evidence: test:tests/test_manager.py::test_create_does_not_wait_for_initial_turn]
- AC20: A vetoed initial turn emits an error event and the session stays ready [evidence: test:tests/test_manager.py::test_initial_turn_veto_keeps_session_ready]
- AC21: Snapshots and the state filter use the public state vocabulary [evidence: test:tests/test_manager.py::test_snapshot_state_vocabulary]

- AC22: A secret override is absent after resume unless it is resent in the resume env [evidence: test:tests/test_manager.py::test_secret_override_must_be_resent_on_resume]
- AC23: The session callbackUrl is persisted and used as the default callback [evidence: test:tests/test_manager.py::test_session_callback_url_default]

## Files

- `src/claude_code_wrapper/manager.py`
- `src/claude_code_wrapper/prompts.py`
- `tests/test_manager.py`
- `tests/test_prompts.py`

## Interfaces

- Part `session-manager`: `claude_code_wrapper.manager` (`SessionManager`, `CreateSessionRequest`, `TooManySessions`, `CreateVetoed`, `CreateFailed`, `ResumeUnavailable`, `ResumeVetoed`, `SessionNotFound`, `InvalidState`).
- Part `prompt-templates`: `claude_code_wrapper.prompts` (`render_template`, `resolve_prompt`, `list_templates`, `PromptError`).
- Consumers: internal-api, api-sessions, api-turns-events, service-main.

## Expected outcome

The service can hold several independent claude sessions, each fully prepared from config before claude starts, destroyable without losing data, and resumable by id after a restart.

## Steps

- Write `tests/test_prompts.py`; red; implement prompts.py; green.
- Write `tests/test_manager.py` with real components, the fake claude as `claude.binary`, tmp `paths`, command hooks writing to tmp files; red; implement create, then destroy, resume, list/load, reaper, shutdown; green; `mise run lint`.

## Notes

- The manager is the only place that composes the parts; keep each step a small function so failures name their step.
- The internal URL comes from service-main (`http://127.0.0.1:<internal_port>`); tests pass any URL (the relay is not exercised here).
- Mounted mode: several sessions may share one mounted workdir (the workbench uses git worktrees); do not lock it.

- 2026-10-20 run claude-code-wrapper-microser: dependency-failed on event-router, home-seed, settings-render, plugins-and-mise, workdir-init
