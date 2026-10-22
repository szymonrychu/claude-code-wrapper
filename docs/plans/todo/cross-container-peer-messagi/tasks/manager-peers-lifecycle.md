---
title: Manager peers lifecycle
status: todo
repo: .
value: Every ccw session joins the shared peer mesh on create and leaves it on every stop path
files: [src/claude_code_wrapper/manager.py, src/claude_code_wrapper/cli.py, tests/test_peers_e2e.py, tests/test_peers_startup.py]
verify: "uv run pytest tests/test_peers_e2e.py tests/test_peers_startup.py tests/test_manager.py tests/test_mcp_injection.py tests/test_cli.py -q && mise run lint"
depends_on: [peers-mcp-tool, peer-session-bootstrap, fake-claude-peer-socket]
owns: []
uses: [peers-config, peer-directory, peer-session-bootstrap, ccw-mcp-peers-tool, fake-peer-socket, peers-dir-fixture]
model: sonnet
estimate: 90
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

Wire peers into the session lifecycle and service start (design D2 to D6). Uses every part the earlier tasks built; adds no new public module.

- `SessionManager.__init__` (manager.py): when `cfg.peers.enabled`, `self.peers = PeerDirectory(cfg.peers)` and `self.peers.prepare()` runs at once, so a bad peers dir stops the service before any session exists (`PeersError` propagates out of `run_service`); disabled leaves `self.peers = None` and touches no filesystem path.
- `_bootstrap` (manager.py:497): `ccw_mcp = bool(agent allowlist) or self.peers is not None` (the ccw MCP server is injected whenever peers are on, so the `peers` tool is reachable with an empty allowlist); `write_instructions(..., peers=self.peers is not None)`; `render_settings(..., peers=self.peers is not None)`.
- `_child_env` (manager.py:616): when enabled, `peer_env = {"CLAUDE_CODE_HARBOR_KITE": "1", "CLAUDE_CODE_SESSION_NAME": peer_session_name(self.peers.instance, meta["name"], session_id)}`; disabled passes None.
- `_wire` (manager.py:632): `SessionSpec(messaging_socket_path=self.peers.socket_path(session_id))` when enabled, else `""`.
- `_launch` (manager.py:528): after the onCreate/onResume gate passed and before the session is marked running, a step `publish_peer` calls `self.peers.publish(session_id=..., name=<CLAUDE_CODE_SESSION_NAME value>, cwd=str(ctx.workdir), started_at=now_iso())` when enabled; a failure there fails the create or resume like any other step.
- `_teardown` (manager.py:824): after claude is stopped, `self.peers.remove(session_id)` when enabled. Every path that frees a slot (destroy, failed or vetoed create, retire of a dead session, abandon, shutdown) goes through `_teardown`, so no entry or socket outlives its session.
- cli.py `_serve`: `PeersError` from `run_service` prints `ccw: peers: <message>` to stderr and returns 1.
- `len(SessionManager._new_session_id(...)) == config.SESSION_ID_LEN` is pinned by a test, so the config-load socket length check stays right.
- tests/test_peers_e2e.py (World from tests/conftest.py, `peers_dir` fixture, the fake claude) with two managers A and B on the same peers dir and different `peers.name`:
  - a session in each: argv has `--messaging-socket-path <dir>/socks/<id>.sock`; the fake's invocation log shows `CLAUDE_CODE_HARBOR_KITE=1` and `CLAUDE_CODE_SESSION_NAME=<instance>-<session name>`; settings.json has `crossSessionInbound: accept` and `mcp__ccw__peers` allowed; `.claude.json` has `mcpServers.ccw` with an empty agent allowlist; CLAUDE.md has the peers paragraph; `<dir>/directory/<id>.json` holds the six keys.
  - a turn in A whose scenario step is `{"mcp": {"server": "ccw", "tool": "peers", "arguments": {}}}` completes and its tool result (transcript) lists B's session id and `uds:` address and not A's own.
  - destroying B removes its entry and socket, and a second peers call from A lists nobody.
  - a disabled config (no `peers` section) produces argv without the flag, no `CLAUDE_CODE_HARBOR_KITE`/`CLAUDE_CODE_SESSION_NAME`, settings.json without `crossSessionInbound` or `mcp__ccw__peers`, no `mcpServers.ccw` with an empty allowlist, no peers paragraph, and creates no peers dir.
- tests/test_peers_startup.py: `SessionManager` with a group-writable non-sticky peers dir raises `PeersError`; `cli.main(["serve", "--config", <file>])` with such a dir exits 1 with `ccw: peers:` and the dir on stderr (config with an absolute fake claude binary, mise install off, port 0).

Interfaces: no new public names; consumes `PeerDirectory`, `peer_session_name`, `SessionSpec.messaging_socket_path`, `build_env(peer_env=)`, `render_settings(peers=)`, `write_instructions(peers=)`, `handle_message(peers=)`.

Notes: World.cfg passes a `peers={...}` section through unchanged (tests/conftest.py `cfg(**sections)`); World forwards `/internal/*` to `create_internal_app(m)`, which reads `m.peers`. Two managers in one World share `sessions_root`; session ids are unique so that is fine, but do not call `load_persisted` on both.

## Acceptance conditions

- AC1: a peers-enabled session launches with the socket flag, peer env, accept setting, ccw MCP server and peers paragraph, and publishes its entry [evidence: test:tests/test_peers_e2e.py::test_enabled_session_wiring]
- AC2: a session in manager A lists the session of manager B through the ccw peers tool, and not itself [evidence: test:tests/test_peers_e2e.py::test_peers_tool_lists_other_manager]
- AC3: destroying a session removes its entry and socket and it disappears from the listing [evidence: test:tests/test_peers_e2e.py::test_destroy_unpublishes]
- AC4: with peers disabled argv, env, settings.json, .claude.json and CLAUDE.md carry nothing peer-related and no peers dir is created [evidence: test:tests/test_peers_e2e.py::test_disabled_changes_nothing]
- AC5: a failed create and a shutdown leave no entry behind [evidence: test:tests/test_peers_e2e.py::test_failed_create_and_shutdown_unpublish]
- AC6: an insecure peers dir makes SessionManager raise PeersError and ccw serve exit 1 with a ccw: peers: message [evidence: test:tests/test_peers_startup.py::test_insecure_dir_refuses_start]
- AC7: new session ids are SESSION_ID_LEN long [evidence: test:tests/test_peers_startup.py::test_session_id_length_matches_config]
- AC8: the existing manager, lifecycle, resume, MCP injection, cli and service suites stay green [evidence: command:uv run pytest tests/test_manager.py tests/test_e2e_lifecycle.py tests/test_e2e_resume.py tests/test_mcp_injection.py tests/test_cli.py tests/test_service.py -q]

## Files

- `src/claude_code_wrapper/manager.py`
- `src/claude_code_wrapper/cli.py`
- `tests/test_peers_e2e.py`
- `tests/test_peers_startup.py`

## Interfaces

No new public names; consumes the parts listed in the Requirement's Interfaces paragraph.

## Expected outcome

Two managers sharing a peers dir see each other's sessions through the ccw peers tool; entries appear on create and resume and vanish on every stop path; an insecure dir stops the service with a clear message; peers off changes nothing.

## Steps

- Write the tests named in the acceptance conditions in `tests/test_peers_e2e.py tests/test_peers_startup.py` first; run `uv run pytest tests/test_peers_e2e.py tests/test_peers_startup.py -q` and see them fail for the missing behaviour (not an import typo).
- Implement the smallest change that makes them pass; run `uv run pytest tests/test_peers_e2e.py tests/test_peers_startup.py -q` and expect all passed.
- Run the task verify: `uv run pytest tests/test_peers_e2e.py tests/test_peers_startup.py tests/test_manager.py tests/test_mcp_injection.py tests/test_cli.py -q && mise run lint`; expect pytest all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

See the Requirement's Notes paragraph.
