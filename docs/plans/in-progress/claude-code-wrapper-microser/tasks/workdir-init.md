---
title: Workdir init
status: in-progress
repo: claude-code-wrapper
value: "Sessions start in an empty dir, a mounted tree or a fresh clone"
files: [src/claude_code_wrapper/workdir.py, tests/test_workdir.py]
verify: "uv run pytest tests/test_workdir.py -q && mise run lint"
depends_on: [config-models]
owns: [workdir-init]
uses: [config-schema]
model: ""
estimate: 75
actual: ""
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/claude-code-wrapper-microser--workdir-init
branch: claude-code-wrapper-microser--workdir-init
verified_head: ""
run: ""
provides: []
---

# Workdir init

## Requirement

Build session workdir preparation and the turn file guard (D4, D11) in `src/claude_code_wrapper/workdir.py`.

- `sanitize_name(name) -> str`: `[A-Za-z0-9._-]` only, no leading dot, not `.`/`..`, max 64 chars; invalid raises `WorkdirError`.
- `async prepare_workdir(init_cfg, workdir_root, *, session_id, name=None, path_override=None, env) -> Path`:
  - mode `empty`: `<workdir_root>/<sanitize(name or init.workdir.name or session_id)>`, created when absent.
  - mode `mounted`: `path_override` or `init.workdir.path`; must be an existing absolute directory; never modified.
  - mode `clone`: target `<workdir_root>/<name>`; absent -> `git clone [--depth N when > 0] [--branch B when set] <repo_url> <target>`, then `git submodule update --init --recursive` when `submodules`; present and a git repo whose `origin` equals repo_url -> reused as is (resume); present with anything else -> `WorkdirError`.
  - git runs with `GIT_TERMINAL_PROMPT=0` and the session env (including `GIT_CONFIG_GLOBAL`); stderr is captured and redacted (credentials in URLs masked) before it appears in errors or logs.
- `write_gitconfig(session_dir, git_cfg, workdir) -> Path`: writes `<session_dir>/gitconfig` with `[user] name/email` when set and `[safe] directory = <workdir>`; returns the path (the session sets `GIT_CONFIG_GLOBAL` to it).
- `async run_setup_commands(commands, workdir, env, marker_path) -> None`: runs each command with `sh -c` in the workdir with its timeout, in order, once per session (skipped when `marker_path` exists, written after success); a failure raises `WorkdirError` with the command index, exit code and the last 2000 chars of redacted output; a timeout kills the process group.
- `safe_write_files(workdir, files: dict[str, str], *, max_bytes, max_files) -> list[str]`: relative paths only; the resolved path (after `realpath`, so symlinks are followed) must stay inside the workdir; more than `max_files` or any content over `max_bytes` (UTF-8) raises `FileWriteError` with `code` `too_large` or `invalid_path`; creates parent dirs; returns the written relative paths.

## Acceptance conditions

- AC1: Mode empty creates the named dir under workdir_root [evidence: test:tests/test_workdir.py::test_empty_mode_creates_dir]
- AC2: Mode mounted requires an existing absolute directory and leaves it unchanged [evidence: test:tests/test_workdir.py::test_mounted_mode_requires_existing_dir]
- AC3: Mode clone clones the branch with depth [evidence: test:tests/test_workdir.py::test_clone_mode_clones_branch]
- AC4: Submodules are initialised recursively when configured [evidence: test:tests/test_workdir.py::test_clone_with_submodules]
- AC5: An existing clone of the same origin is reused [evidence: test:tests/test_workdir.py::test_clone_reuses_existing_same_origin]
- AC6: A foreign existing directory is rejected [evidence: test:tests/test_workdir.py::test_clone_rejects_foreign_dir]
- AC7: Names are sanitised and traversal rejected [evidence: test:tests/test_workdir.py::test_name_sanitised]
- AC8: Setup commands run once and a failure reports index, exit code and redacted tail [evidence: test:tests/test_workdir.py::test_setup_commands_run_once_and_fail_with_tail]
- AC9: The session gitconfig carries identity and safe.directory [evidence: test:tests/test_workdir.py::test_gitconfig_written]
- AC10: safe_write_files rejects absolute, traversal and symlink escapes [evidence: test:tests/test_workdir.py::test_safe_write_rejects_escape_and_symlink]
- AC11: safe_write_files enforces size and count caps [evidence: test:tests/test_workdir.py::test_safe_write_caps]
- AC12: Credentials in clone errors are redacted [evidence: test:tests/test_workdir.py::test_clone_error_redacts_credentials]

## Files

- `src/claude_code_wrapper/workdir.py`
- `tests/test_workdir.py`

## Interfaces

- Part `workdir-init`: `claude_code_wrapper.workdir` (`prepare_workdir`, `write_gitconfig`, `run_setup_commands`, `safe_write_files`, `sanitize_name`, `WorkdirError`, `FileWriteError`). Consumers: session-manager, event-router (turn files).

## Expected outcome

A session can start in an empty dir, a mounted workbench tree at its fixed absolute path, or a fresh clone with submodules, with setup commands run once and turn file uploads confined to the workdir.

## Steps

- Write `tests/test_workdir.py` using real local git repos (`git init --bare` origins, a submodule repo) built with subprocess; red.
- Implement; green; `mise run lint`.

## Notes

- Sources: tatara `W/internal/bootstrap/bootstrap.go:591-894` (clone, submodules, git stderr never logged raw), magic `M/api.py:46-80` (file guard, 256 KiB).
- Workbench needs the tree root as cwd and a fixed absolute path (task files store absolute worktree paths); never move a mounted workdir.
- Use `git -c protocol.file.allow=always` for local submodule clones in tests (git blocks file transport for submodules by default).
