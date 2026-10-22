---
title: Manager peers lifecycle
status: todo
repo: .
value: Every ccw session joins the peer mesh on create and leaves it on every stop path
files: [src/claude_code_wrapper/manager.py, src/claude_code_wrapper/service.py, src/claude_code_wrapper/cli.py, tests/test_peers_lifecycle.py, tests/test_peers_startup.py]
verify: "uv run pytest tests/test_peers_lifecycle.py tests/test_peers_startup.py tests/test_manager.py tests/test_mcp_injection.py tests/test_cli.py tests/test_service.py -q && mise run lint"
depends_on: [peer-service-core, peer-session-bootstrap]
owns: []
uses: [peers-config, peer-service, peer-session-bootstrap, peer-inbox, peer-messages-endpoint, ccw-mcp-peers-tool]
model: sonnet
estimate: 75
actual: ""
review: full
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Manager peers lifecycle

## Requirement

Wire the peer service into the session manager and the service start (design D1, D2, D3, D5, D8 shutdown). No new public module.

- src/claude_code_wrapper/manager.py:
  - `SessionManager.peers: PeerService | None`: None when `cfg.peers.enabled` is false, and then no peers path is touched. `async start_peers(public_port: int)` builds and starts the `PeerService` when enabled (a `PeersError` propagates); `create` and `resume` with peers enabled but not started raise `InvalidState` (`peers not started`).
  - `_bootstrap`: `ccw_mcp = bool(agent allowlist) or peers enabled`, so the ccw MCP server is injected with an empty allowlist when peers are on; `write_instructions(..., peers=enabled)`; `render_settings(..., peers=enabled)`.
  - `_child_env`: `build_env(..., peers=enabled)`.
  - `_launch`: after the onCreate/onResume gate passed and before the session is marked running, a step `publish_peer` awaits `self.peers.register(session_id, ctx.router, name=<session name or id>, cwd=str(ctx.workdir), started_at=<now, ms precision>)`; a failure fails the create or resume like any other step. A vetoed or failed create never reaches this step, so it never writes an entry.
  - `_teardown`: after claude stopped and before the router closes, `await self.peers.unregister(session_id)` when the session was registered. Every slot-freeing path (destroy, failed create after publish, retire, abandon, reaper, shutdown) runs `_teardown`, so no entry outlives its session.
  - `shutdown`: after the sessions are down, `await self.peers.stop(drain_timeout=<what is left of the shutdown budget, at least 0>)`, so dropped-message notices get their chance to reach senders within the bound.
- src/claude_code_wrapper/service.py `_run`: after both ports are bound and before `load_persisted`, `await manager.start_peers(handle.public_port)`.
- src/claude_code_wrapper/cli.py `_serve`: a `PeersError` from `run_service` prints `ccw: peers: <message>` to stderr and returns 1.
- tests/test_peers_lifecycle.py (World, fake claude, a tmp peers directory, `await m.start_peers(<port>)`): an enabled session has `CLAUDE_CODE_HARBOR_KITE=0` in the fake's logged env, SendMessage/ListAgents denied and the peer tools allowed in settings.json, `mcpServers.ccw` in .claude.json with an empty allowlist, the peers paragraph in CLAUDE.md and its signed directory entry; a turn whose scenario calls `mcp__ccw__peers` lists the session of a second manager (another instance name, same directory) and not itself, read from the session's `fake-mcp.jsonl`; destroy and shutdown each remove the entry; a create vetoed by onCreate never writes an entry at all (a watcher on the instance dir, or a spy on `PeerDirectory.publish`, sees no write during the whole create); with peers disabled argv, env, settings.json, .claude.json and CLAUDE.md carry nothing peer-related and no peers directory is created.
- tests/test_peers_startup.py: `ccw serve --config <file>` with peers enabled and an uncreatable directory (a path under a regular file) exits 1 with `ccw: peers:` on stderr; a stale or live entry under our own name from an earlier run is swept at startup with a warning and the service starts; `run_service` with peers enabled serves `POST /v1/peers/messages` (401 unsigned) and with peers disabled does not (404).

Interfaces: `SessionManager.peers`, `SessionManager.start_peers` (consumed by service.py and by `api.peers` / `internal_api` through `manager.peers`).

Notes: two managers in one World share `sessions_root`; session ids are unique so that is fine, but do not call `load_persisted` on both. World forwards `/internal/*` to `create_internal_app(m)`, which reads `m.peers`.

## Acceptance conditions

- AC1: a peers-enabled session gets HARBOR_KITE=0, the deny and allow rules, the ccw MCP server, the peers paragraph and its signed directory entry [evidence: test:tests/test_peers_lifecycle.py::test_enabled_session_wiring]
- AC2: a session of manager A lists the session of manager B through mcp__ccw__peers and not itself [evidence: test:tests/test_peers_lifecycle.py::test_peers_tool_lists_other_manager]
- AC3: destroy and shutdown each remove the directory entry [evidence: test:tests/test_peers_lifecycle.py::test_stop_paths_unpublish]
- AC4: a create vetoed by onCreate never writes a directory entry [evidence: test:tests/test_peers_lifecycle.py::test_vetoed_create_never_publishes]
- AC5: with peers disabled nothing peer-related is written and no peers directory is created [evidence: test:tests/test_peers_lifecycle.py::test_disabled_changes_nothing]
- AC6: ccw serve exits 1 with a ccw: peers: message when the directory cannot be created [evidence: test:tests/test_peers_startup.py::test_uncreatable_directory_refuses_start]
- AC7: leftover entries under our own name are swept at startup and the service starts [evidence: test:tests/test_peers_startup.py::test_own_leftovers_swept]
- AC8: the peer route is served only when peers are enabled [evidence: test:tests/test_peers_startup.py::test_peer_route_mounted_only_when_enabled]
- AC9: the existing manager, lifecycle, resume, injection, cli and service suites stay green [evidence: command:uv run pytest tests/test_manager.py tests/test_e2e_lifecycle.py tests/test_e2e_resume.py tests/test_mcp_injection.py tests/test_cli.py tests/test_service.py -q]

## Files

- `src/claude_code_wrapper/manager.py`
- `src/claude_code_wrapper/service.py`
- `src/claude_code_wrapper/cli.py`
- `tests/test_peers_lifecycle.py`
- `tests/test_peers_startup.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
