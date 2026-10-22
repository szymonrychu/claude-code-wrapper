---
title: Manager peers lifecycle
status: in-progress
repo: .
value: Every ccw session is registered with the peer mesh while it runs
files: [src/claude_code_wrapper/manager.py, tests/test_peers_lifecycle.py]
verify: "uv run pytest tests/test_peers_lifecycle.py tests/test_manager.py tests/test_mcp_injection.py tests/test_e2e_lifecycle.py -q && mise run lint"
depends_on: [peer-service-core, peer-session-bootstrap, peer-membership-hooks]
owns: []
uses: [peers-config, peer-service, peer-session-bootstrap, peer-inbox, peer-messages-endpoint, ccw-mcp-peers-tool, peer-membership-hooks]
model: sonnet
estimate: 60
actual: ""
review: full
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/cross-container-peer-messagi--manager-peers-lifecycle
branch: cross-container-peer-messagi--manager-peers-lifecycle
verified_head: ""
run: ""
provides: []
---

# Manager peers lifecycle

## Requirement

Wire the peer service into the session manager (design D2, D3, D5 session list). Startup and shutdown ordering and the CLI flag are the next task (peers-join-leave-order).

- src/claude_code_wrapper/manager.py:
  - `SessionManager.peers: PeerService | None`: None when `cfg.peers.enabled` is false, and then no peer hook fires and no peer traffic happens. `async start_peers(public_port)` builds the service (with `hooks.instance_dispatcher(...)`) and awaits `peers.join()`; until it returns, `create` and `resume` raise `InvalidState` (`peers joining`). `async leave_peers()` awaits `peers.leave()` once (idempotent). `async stop_peers(drain_timeout)` awaits `peers.stop(...)`.
  - `_bootstrap`: `ccw_mcp = bool(agent allowlist) or peers enabled`; `write_instructions(..., peers=enabled)`; `render_settings(..., peers=enabled)`. `_child_env`: `build_env(..., peers=enabled)`.
  - `_launch`: after the onCreate/onResume gate passed and before the session is marked running, step `publish_peer` calls `self.peers.register(session_id, ctx.router, name=<session name or id>, cwd=str(ctx.workdir))`. A vetoed or failed create never reaches it.
  - `_teardown`: after claude stopped and before the router closes, `self.peers.unregister(session_id)` when registered.
- tests/test_peers_lifecycle.py (World, fake claude, two managers each with `await start_peers(...)`, reaching each other through the `ccw_service` fixture or an `httpx` transport to each other's app): an enabled session has `CLAUDE_CODE_HARBOR_KITE=0` in the fake's logged env, the deny and allow rules, `mcpServers.ccw` with an empty allowlist and the peers paragraph; a turn whose scenario calls `mcp__ccw__peers` lists the other manager's session and not itself (read from `fake-mcp.jsonl`); destroy removes the session from the other side's listing after the immediate heartbeat; a create vetoed by onCreate is never registered; a create while joining is rejected; with peers disabled nothing peer-related is written, no peer hook fires (a recording action on all four stays untouched) and no peer request is made.

Interfaces: `SessionManager.peers`, `start_peers`, `leave_peers`, `stop_peers` (consumed by service.py, `api.peers` and `internal_api` through `manager.peers`).

Notes: two managers in one World share `sessions_root`; session ids are unique so that is fine, but do not call `load_persisted` on both. World forwards `/internal/*` to `create_internal_app(m)`, which reads `m.peers`.

## Acceptance conditions

- AC1: a peers-enabled session gets HARBOR_KITE=0, the deny and allow rules, the ccw MCP server and the peers paragraph [evidence: test:tests/test_peers_lifecycle.py::test_enabled_session_wiring]
- AC2: a session of manager A lists the session of manager B through mcp__ccw__peers and not itself [evidence: test:tests/test_peers_lifecycle.py::test_peers_tool_lists_other_manager]
- AC3: a destroyed session disappears from the other manager's listing [evidence: test:tests/test_peers_lifecycle.py::test_destroy_unregisters]
- AC4: a create vetoed by onCreate is never registered and a create while joining is rejected [evidence: test:tests/test_peers_lifecycle.py::test_vetoed_and_early_creates]
- AC5: with peers disabled nothing peer-related is written, no peer hook fires and no peer request is made [evidence: test:tests/test_peers_lifecycle.py::test_disabled_changes_nothing]
- AC6: the existing manager, lifecycle, resume and injection suites stay green [evidence: command:uv run pytest tests/test_manager.py tests/test_e2e_lifecycle.py tests/test_e2e_resume.py tests/test_mcp_injection.py -q]

## Files

- `src/claude_code_wrapper/manager.py`
- `tests/test_peers_lifecycle.py`


## Interfaces

See the Requirement's Interfaces paragraph; keep every name and signature stated there.

## Expected outcome

Every ccw session is registered with the peer service while it runs and peers off changes nothing.

## Steps

- Write the tests named in the acceptance conditions in `tests/test_peers_lifecycle.py` first; run `uv run pytest tests/test_peers_lifecycle.py -q` and see them fail for the missing behaviour (not an import typo).
- Implement the smallest change that makes them pass; run `uv run pytest tests/test_peers_lifecycle.py -q` again and expect all passed.
- Run the task verify: `uv run pytest tests/test_peers_lifecycle.py tests/test_manager.py tests/test_mcp_injection.py tests/test_e2e_lifecycle.py -q && mise run lint`; expect all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

See the Requirement's Notes paragraph.
