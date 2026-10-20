---
title: Session boot
status: todo
repo: claude-code-wrapper
value: "claude boots unattended, recovers from crashes and never sees service secrets"
files: [src/claude_code_wrapper/session.py, tests/test_session_boot.py]
verify: "uv run pytest tests/test_session_boot.py -q && mise run lint"
depends_on: [pty-process, config-models]
owns: [session]
uses: [pty-process, config-schema, claude-env, fake-claude]
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

# Session boot

## Requirement

Build the session process lifecycle (D1, D4, D9, D13) in `src/claude_code_wrapper/session.py`: spawn, boot handshake, readiness, relaunch, fatal detection, stop.

- `SessionSpec` dataclass: `session_id`, `claude_session_id` (uuid), `workdir`, `config_dir`, `session_dir`, `binary`, `model`, `effort`, `extra_args`, `env: dict[str, str]` (fully built child env), `boot` (config boot section), `max_restarts`, `resume: bool`.
- `build_argv(spec) -> list[str]`: `[binary, --model M (when set), --effort E (when set), --resume <claude_session_id> when spec.resume or a transcript already exists, else --session-id <claude_session_id>, *extra_args]`; raises `ValueError` if `--dangerously-skip-permissions` is present.
- `build_env(*, service_env, claude_env: ClaudeEnv, config_dir, gitconfig, session_id, internal_url, hook_token, gate_fail, gate_timeout, mise_env, passthrough) -> dict[str, str]`: starts from ONLY `PATH`, `HOME`, `LANG`, `LC_ALL`, `USER`, `SHELL`, `TMPDIR` copied from `service_env` when present; adds `TERM=xterm-256color`, `claude_env.to_env()`, `CLAUDE_CONFIG_DIR=<config_dir>`, `CLAUDE_CODE_PROJECT_DIR_NAME=workdir`, `GIT_CONFIG_GLOBAL=<gitconfig>`, `CCW_SESSION_ID`, `CCW_INTERNAL_URL`, `CCW_HOOK_TOKEN`, `CCW_TOOL_GATE_FAIL` (`allow`/`deny`), `CCW_GATE_TIMEOUT`, `mise_env`, and every name in `passthrough` that exists in `service_env`. Nothing else from the service env (in particular no `CCW_AUTH_TOKEN` or other service secrets) reaches the child.
- `SessionListener` protocol: `on_state(state)`, `on_output(text_chunk)`, `on_fatal(text, tail)`, `on_restart(count, resumed)`, `on_exit(code)`.
- `Session(spec, listener, *, clock=time.monotonic)`; `state` in `booting|ready|busy|stopping|stopped|dead`.
  - `async start()`: spawns a `PtyProcess` with `build_argv`/`spec.env` in the workdir; boot wait polls the ring every 0.15 s: each configured dialog handler fires at most once per boot when its pattern appears (wait `delay_seconds`, then write each key with 0.25 s between keys); ready when (a ready marker is visible or output has been idle `idle_ready_seconds`) and at least `min_boot_seconds` elapsed and no dialog pattern is still pending; `timeout_seconds` without ready -> close, `ring.reset()`, relaunch once; a second timeout -> state `dead`, `BootError("boot_timeout", tail)` with the redacted last 800 chars of the screen.
  - Fatal patterns from config are checked on every output chunk (ANSI stripped, case-insensitive); a match calls `on_fatal` once per occurrence and records `last_fatal`.
  - Unexpected exit while ready or busy: when restarts < `max_restarts` relaunch (with `--resume` only if the transcript file exists, else `--session-id`; ring reset first), state goes booting then ready, `on_restart(n, resumed)`; otherwise state `dead` and `on_exit(code)`.
  - `async stop(grace=5.0)`: state stopping, PTY close, state stopped; no relaunch afterwards.
  - `snapshot() -> dict`: state, pid, restarts, last_fatal, started_at, ready_at.
  - Provide `pty` (the PtyProcess) and `transcript_path` attributes for session-turns.

## Acceptance conditions

- AC1: A new session's argv uses --session-id with model and effort [evidence: test:tests/test_session_boot.py::test_argv_new_session_uses_session_id]
- AC2: A resumed session's argv uses --resume with the same id [evidence: test:tests/test_session_boot.py::test_argv_resume_uses_resume]
- AC3: --dangerously-skip-permissions is refused [evidence: test:tests/test_session_boot.py::test_argv_rejects_dangerous_flag]
- AC4: The child env contains the minimal base plus managed and CCW vars [evidence: test:tests/test_session_boot.py::test_env_minimal_and_managed_vars]
- AC5: Service secrets never reach the child env [evidence: test:tests/test_session_boot.py::test_env_excludes_service_secrets]
- AC6: Boot answers the bypass dialog and reaches ready [evidence: test:tests/test_session_boot.py::test_boot_accepts_bypass_dialog_and_becomes_ready]
- AC7: Boot timeout relaunches once then marks the session dead with a tail [evidence: test:tests/test_session_boot.py::test_boot_timeout_relaunches_once_then_dead]
- AC8: A crash relaunches with --resume when the transcript exists [evidence: test:tests/test_session_boot.py::test_crash_relaunches_with_resume_when_transcript_exists]
- AC9: An exhausted restart budget marks the session dead [evidence: test:tests/test_session_boot.py::test_restart_budget_exhausted_dead]
- AC10: A fatal pattern on screen is reported to the listener [evidence: test:tests/test_session_boot.py::test_fatal_pattern_reported]
- AC11: stop terminates claude and prevents relaunch [evidence: test:tests/test_session_boot.py::test_stop_kills_process]
- AC12: The ring is reset before a relaunch so stale dialog text cannot match [evidence: test:tests/test_session_boot.py::test_ring_reset_on_relaunch]

## Files

- `src/claude_code_wrapper/session.py`
- `tests/test_session_boot.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
