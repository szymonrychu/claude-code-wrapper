---
title: Passthrough env globs
status: done
repo: .
value: "Arbitrary operator env names reach claude by pattern, with excludes for secrets (K11)"
files: [src/claude_code_wrapper/config.py, src/claude_code_wrapper/session.py, src/claude_code_wrapper/manager.py, README.md, tests/test_passthrough_globs.py]
verify: "uv run pytest tests/test_passthrough_globs.py tests/test_claude_env.py tests/test_session_boot.py tests/test_readme_config.py -q && mise run lint"
depends_on: [outstanding-subagent-count]
owns: [passthrough-globs]
uses: []
model: sonnet
estimate: 60
actual: 3
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/hook-capabilities-for-tatara--passthrough-env-globs
branch: hook-capabilities-for-tatara--passthrough-env-globs
verified_head: f23b9ba8ac2e54465496801c9ccd461321f7ad8b
run: hook-capabilities-for-tatara
provides: [passthrough-globs]
acs_for_audit: []
process_gaps: []
---

# Passthrough env globs

## Requirement

K11 passthrough env globs (design D15). Owns part `passthrough-globs`.

- `claude.passthrough_env` entries are `fnmatch.fnmatchcase` patterns (a plain name still matches only itself). New `claude.passthrough_env_exclude: list[str]` patterns remove matches.
- `CCW_*` names and the service-managed names are never passed, even through `*`; the refusal is logged (as today) without failing.
- One shared function selects the names (e.g. `passthrough_names(service_env, patterns, exclude)` in session.py) and both `build_env` and `SessionManager._tool_env` use it, so claude and setup commands see the same set.
- README: the config rows, glob semantics, and a warning that `*` hands every service secret to claude (use explicit globs plus excludes).

## Acceptance conditions

- AC1: glob patterns select matching service env names [evidence: test:tests/test_passthrough_globs.py::test_glob_matches_names]
- AC2: passthrough_env_exclude removes matches [evidence: test:tests/test_passthrough_globs.py::test_exclude_removes_matches]
- AC3: CCW_* and managed names never pass, even with * [evidence: test:tests/test_passthrough_globs.py::test_ccw_and_managed_never_pass_star]
- AC4: the tool env for setup commands uses the same rule [evidence: test:tests/test_passthrough_globs.py::test_tool_env_uses_same_rule]
- AC5: plain names behave as before [evidence: test:tests/test_passthrough_globs.py::test_plain_names_unchanged]
- AC6: README documents the keys [evidence: command:uv run pytest tests/test_readme_config.py tests/test_claude_env.py -q]

## Files

- `src/claude_code_wrapper/config.py`
- `src/claude_code_wrapper/session.py`
- `src/claude_code_wrapper/manager.py`
- `README.md`
- `tests/test_passthrough_globs.py`

## Interfaces

- Part `passthrough-globs`: `ClaudeConfig.passthrough_env` (patterns), `ClaudeConfig.passthrough_env_exclude`, one selector function in session.py used by `build_env` and `SessionManager._tool_env`.

## Expected outcome

Operators pass arbitrary env families to claude by pattern without ever leaking CCW_* or managed names.

## Steps

- Write `tests/test_passthrough_globs.py` (pure unit tests on `build_env` and `_tool_env`); see it fail.
- Implement the selector and use it in both places; add the exclude key.
- README rows and warning.
- `uv run pytest tests/test_passthrough_globs.py tests/test_claude_env.py tests/test_session_boot.py tests/test_readme_config.py -q && mise run lint`.

## Notes

- Today `_tool_env` and `build_env` filter differently (managed names); the shared selector removes that drift.
