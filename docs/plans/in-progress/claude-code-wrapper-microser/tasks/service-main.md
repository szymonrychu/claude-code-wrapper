---
title: Service main
status: in-progress
repo: claude-code-wrapper
value: ccw serve runs the service and shuts down cleanly
files: [src/claude_code_wrapper/service.py, src/claude_code_wrapper/cli.py, src/claude_code_wrapper/__main__.py, tests/test_cli.py, tests/test_service.py]
verify: "uv run pytest tests/test_cli.py tests/test_service.py -q && mise run lint"
depends_on: [metrics, internal-api]
owns: [service-cli]
uses: [public-api, internal-api, session-manager, mise-tooling, metrics]
model: ""
estimate: 75
actual: ""
review: full
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/claude-code-wrapper-microser--service-main
branch: claude-code-wrapper-microser--service-main
verified_head: ""
run: claude-code-wrapper-microser
provides: []
acs_for_audit: []
process_gaps: []
---

# Service main

## Requirement

Build the process entry point (D8, D9, D12) in `src/claude_code_wrapper/service.py`, `cli.py` and `__main__.py`.

service.py:
- `async run_service(cfg, *, ready=None, stop=None) -> None`: installs a global log redactor; runs `tooling.render_global_mise` + `install_global` when configured (ToolingError with `mise.required` aborts with exit code 1 at the CLI); creates `Metrics`, the redactor, `SessionManager` (internal URL `http://127.0.0.1:<internal_port>`), `await manager.load_persisted()`; builds `create_app` and `create_internal_app`; runs two `uvicorn.Server`s in this loop (public on `server.host:server.port`, internal on `127.0.0.1:server.internal_port`, `timeout_graceful_shutdown=3`, uvicorn's own signal handlers disabled); sets `ready` (an `asyncio.Event`, when given) once both are listening and exposes the bound ports (port 0 support for tests) on a returned/attached handle; on `stop` (event) or SIGTERM/SIGINT: `manager.shutdown()` bounded by `limits.shutdown_timeout_seconds`, dispatcher drain bounded, then both servers exit.
- Logging: JSON lines on stdout (`ts, level, logger, msg` plus extras) at `server.log_level`; a logging filter redacts every message and arg with the global redactor.

cli.py (`main(argv=None) -> int`, also `python -m claude_code_wrapper`):
- `ccw serve [--config PATH] [--host H] [--port P]` (`--config` defaults to `$CCW_CONFIG`, else `/etc/ccw/config.yaml`): load config; `ConfigError` -> message on stderr, exit 78; otherwise run `run_service`; exit 0 after a clean shutdown.
- `ccw check-config --config PATH`: prints `cfg.redacted()` as JSON and exits 0; invalid -> stderr message and exit 78.
- `ccw version`: prints `__version__`.

Settled by the builder check (binding):
- Both `server.port` and `server.internal_port` may be 0. Sockets are bound first; then `ServiceHandle(public_port, internal_port)` is filled with the real ports and `ready` is set; the manager's internal URL is derived from the bound internal port after binding.

Settled at the gate (D19-D24, binding):
- `ccw serve` reads `--config`, else `$CCW_CONFIG`, else `/etc/ccw/config.yaml`.
- Readiness: `manager.started` is set only after config load, global mise install, `load_persisted()` and both listeners bound; until then `/readyz` is 503 while `/healthz` is 200.
- SIGTERM grace is `limits.shutdown_timeout_seconds`; logs are JSON lines on stdout.

## Acceptance conditions

- AC1: check-config prints the redacted config and exits 0 [evidence: test:tests/test_cli.py::test_check_config_ok_prints_redacted]
- AC2: check-config with an invalid file exits 78 with the error path [evidence: test:tests/test_cli.py::test_check_config_invalid_exit_78]
- AC3: serve with an invalid config exits 78 [evidence: test:tests/test_cli.py::test_serve_invalid_config_exit_78]
- AC4: version prints the package version [evidence: test:tests/test_cli.py::test_version]
- AC5: run_service binds the public port and the internal port on loopback only [evidence: test:tests/test_service.py::test_run_service_binds_public_and_internal_loopback]
- AC6: SIGTERM stops sessions within the bound and the process exits 0 [evidence: test:tests/test_service.py::test_sigterm_shuts_down_sessions_bounded]
- AC7: Log output goes to stdout as JSON lines and is redacted [evidence: test:tests/test_service.py::test_logs_redacted]
- AC8: A required global mise failure aborts startup with exit 1 [evidence: test:tests/test_service.py::test_required_mise_failure_aborts]

- AC9: With both ports 0 the handle reports the bound ports and the internal URL uses the bound internal port [evidence: test:tests/test_service.py::test_port_zero_handle_and_internal_url]

- AC10: ccw serve takes the config path from CCW_CONFIG when --config is absent [evidence: test:tests/test_cli.py::test_config_path_from_ccw_config_env]
- AC11: readyz is 503 until init finished and listeners bound, healthz is 200 throughout [evidence: test:tests/test_service.py::test_readyz_only_after_init_and_bind]
- AC12: Shutdown honours limits.shutdown_timeout_seconds as the grace period [evidence: test:tests/test_service.py::test_shutdown_grace_from_config]

## Files

- `src/claude_code_wrapper/service.py`
- `src/claude_code_wrapper/cli.py`
- `src/claude_code_wrapper/__main__.py`
- `tests/test_cli.py`
- `tests/test_service.py`

## Interfaces

- Part `service-cli`: `claude_code_wrapper.service.run_service`, `claude_code_wrapper.cli.main` (`ccw serve|check-config|version`). Consumers: e2e-lifecycle (in-process service), container (CMD).

## Expected outcome

`ccw serve --config config.example.yaml` serves the API and the loopback hook endpoint, and stops cleanly on SIGTERM without orphaned claude processes.

## Steps

- Write `tests/test_cli.py` (subprocess `uv run ccw ...` or `[sys.executable, "-m", "claude_code_wrapper", ...]`) and `tests/test_service.py` (in-loop `run_service` with port 0; the SIGTERM test runs `ccw serve` as a subprocess with the fake claude, creates a session over HTTP, sends SIGTERM, asserts exit 0 within the bound and session.json state stopped); red.
- Implement; green; `mise run lint`.

## Notes

- Shutdown order (tatara `W/cmd/wrapper/app.go:523-556`): stop accepting, stop sessions, drain hooks, close servers. ros2 sets uvicorn `timeout_graceful_shutdown=3` so open SSE streams cannot hold shutdown.
- Exit 78 (EX_CONFIG) mirrors ros2 so supervisors do not restart-loop on a bad config.

- 2026-10-20 run claude-code-wrapper-microser: dependency-failed on metrics, internal-api
