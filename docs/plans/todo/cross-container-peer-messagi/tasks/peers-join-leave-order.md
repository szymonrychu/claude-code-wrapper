---
title: Peers join leave order
status: todo
repo: .
value: "No session starts before the join and no onDestroy runs before the leave, and seeds can come from the command line"
files: [src/claude_code_wrapper/manager.py, src/claude_code_wrapper/service.py, src/claude_code_wrapper/cli.py, tests/test_peers_startup.py]
verify: "uv run pytest tests/test_peers_startup.py tests/test_service.py tests/test_cli.py tests/test_exit_policy.py tests/test_destroy_io.py -q && mise run lint"
depends_on: [manager-peers-lifecycle]
owns: []
uses: [peers-config, peer-service]
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

# Peers join leave order

## Requirement

Join before any session starts, leave before any onDestroy, and the `--peer` flag (design D2 static, D5 ordering, D15).

- src/claude_code_wrapper/service.py `_run`: after both ports are bound and before `load_persisted` and `manager.started = True`, `await manager.start_peers(handle.public_port)`; at stop, the manager's shutdown handles the leave.
- src/claude_code_wrapper/manager.py:
  - `shutdown`: first `await self.leave_peers()` (preLeave, leave calls, postLeave) before any session's onDestroy, then the existing session shutdown, then `await self.stop_peers(drain_timeout=<rest of the shutdown budget, at least 0>)`. The leave is bounded by `join_timeout_seconds` plus the hook timeouts and its time comes out of the shutdown budget.
  - `exit_when_last_destroyed`: when a destroy will leave no active session under that policy, `leave_peers()` runs before that session's onDestroy (the destroy path checks the policy first). `exit_when_dead` requests exit through the normal shutdown, so the leave also precedes its onDestroy.
- src/claude_code_wrapper/cli.py: `ccw serve --peer URL` (repeatable) extends `cfg.peers.static` through `PeersConfig.with_static`; a bad URL exits 2 with the config error; a `PeersError` (or any peers startup error) from `run_service` prints `ccw: peers: <message>` and returns 1.
- tests/test_peers_startup.py (command hooks writing ordered marker lines to one file, the fake claude, `run_service` on port 0): startup order is preJoin, postJoin, then the onCreate of a session created right after ready; an unreachable seed lets the service become ready within `join_timeout_seconds` plus slack; on stop with a live session the order is preLeave, the leave request at the other member, postLeave, onDestroy; with `exit_when_last_destroyed` destroying the last session gives preLeave, postLeave, onDestroy, then exit; `ccw serve --peer URL --peer URL2` lands both in the static list and a bad `--peer` exits 2; with peers disabled `POST /v1/peers/join` is 404 and no peer hook runs.

Interfaces: `ccw serve --peer` (CLI).

## Acceptance conditions

- AC1: preJoin and postJoin run before any session's onCreate [evidence: test:tests/test_peers_startup.py::test_join_precedes_oncreate]
- AC2: an unreachable seed does not hold startup past join_timeout_seconds [evidence: test:tests/test_peers_startup.py::test_unreachable_seed_bounded]
- AC3: on shutdown preLeave, the leave and postLeave run before onDestroy [evidence: test:tests/test_peers_startup.py::test_leave_precedes_ondestroy]
- AC4: with exit_when_last_destroyed the leave runs before the last session's onDestroy [evidence: test:tests/test_peers_startup.py::test_exit_when_last_destroyed_leaves_first]
- AC5: ccw serve --peer extends the static seed list and a bad URL exits 2 [evidence: test:tests/test_peers_startup.py::test_cli_peer_flag]
- AC6: with peers disabled the join route is absent and no peer hook runs [evidence: test:tests/test_peers_startup.py::test_disabled_no_membership]
- AC7: the existing service, cli, exit policy and shutdown suites stay green [evidence: command:uv run pytest tests/test_service.py tests/test_cli.py tests/test_exit_policy.py tests/test_destroy_io.py -q]

## Files

- `src/claude_code_wrapper/manager.py`
- `src/claude_code_wrapper/service.py`
- `src/claude_code_wrapper/cli.py`
- `tests/test_peers_startup.py`


## Interfaces

See the Requirement's Interfaces paragraph; keep every name and signature stated there.

## Expected outcome

No session starts before the join finished and no onDestroy runs before the leave, on every shutdown path, and seeds can come from the command line.

## Steps

- Write the tests named in the acceptance conditions in `tests/test_peers_startup.py` first; run `uv run pytest tests/test_peers_startup.py -q` and see them fail for the missing behaviour (not an import typo).
- Implement the smallest change that makes them pass; run `uv run pytest tests/test_peers_startup.py -q` again and expect all passed.
- Run the task verify: `uv run pytest tests/test_peers_startup.py tests/test_service.py tests/test_cli.py tests/test_exit_policy.py -q && mise run lint`; expect all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

Hooks write ordered marker lines to one file; assert on that order.
- Builder check: exit_when_last_destroyed leaves only when `_active` would become empty, before `_start_destroy` of that last session; a concurrent create or a vetoed destroy keeps membership; leave is idempotent. exit_when_dead: `_retire` awaits `leave_peers()` before that session's onDestroy runs. Leave targets every reachable member. Joining before the servers listen is accepted: peers' first heartbeats to us may miss and recover on the next one.
