---
title: Peer directory module
status: todo
repo: .
value: "ccw containers sharing one mount can publish, find and liveness-check each other's sessions"
files: [src/claude_code_wrapper/peers.py, tests/test_peers_directory.py]
verify: "uv run pytest tests/test_peers_directory.py -q && mise run lint"
depends_on: [fake-claude-peer-socket, peers-config-section]
owns: [peer-directory]
uses: [peers-config, peers-dir-fixture]
model: sonnet
estimate: 80
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Peer directory module

## Requirement

A new module src/claude_code_wrapper/peers.py: the shared peers directory on the mount, its vetting, the per-session directory entries and the liveness probe (design D4, D5; facts F2, F4, F7). Owns part `peer-directory`, registered as the cross-epic contract `peer-directory-entry`.

- `class PeersError(Exception)`; every message starts with `peers.dir <path>: `.
- `def instance_name(cfg: PeersConfig) -> str`: `cfg.name`, else `socket.gethostname()` with every character outside `[A-Za-z0-9._-]` replaced by `-`.
- `def peer_session_name(instance: str, session_name: str | None, session_id: str) -> str`: `f"{instance}-{x}"` where `x` is the session name (or the session id when the name is empty or None) with characters outside `[A-Za-z0-9._-]` replaced by `-`.
- `class PeerDirectory`, constructed as `PeerDirectory(cfg: PeersConfig, *, uid: int | None = None)` (uid defaults to `os.getuid()`; the parameter exists so tests can simulate a foreign owner). Attributes/properties: `root` (cfg.dir), `socks_dir` (`<dir>/socks`), `entries_dir` (`<dir>/directory`), `instance` (instance_name(cfg)), `probe_timeout` (seconds).
- `socket_path(session_id) -> str` = `f"{dir}/socks/{session_id}.sock"`; `address(session_id) -> str` = `"uds:" + socket_path(session_id)`.
- `prepare() -> None`, synchronous, idempotent:
  - When `dir` is missing, create it (and missing parents) with mode 0700.
  - `dir` must be owned by `uid`; it must not be group- or world-writable unless the sticky bit is set. Otherwise `PeersError` naming the owner uid and octal mode and saying Claude would otherwise fall back to `/tmp/cc-socks-<uid>` silently.
  - Every ancestor of `dir` (each of `Path(dir).parents`, stat following symlinks) must be owned by `uid` or root and must not be group- or world-writable without the sticky bit (mirrors Claude's vetting, fact F2); otherwise `PeersError` naming that ancestor.
  - Create `socks` and `directory` with mode 0700; when they already exist they must be owned by `uid` (else `PeersError`) and are chmod'ed to 0700.
  - Sweep: delete every entry in `directory` whose `instance` equals this instance and whose socket refuses or is missing (synchronous connect with `probe_timeout`). Entries of other instances are never deleted.
- `publish(*, session_id, name, cwd, started_at) -> dict`: writes `<dir>/directory/<session_id>.json` atomically (temp file in the same dir, mode 0600, then `os.replace`) with exactly the keys `session_id, name, instance, address, cwd, started_at`; returns the entry.
- `remove(session_id) -> None`: unlinks the entry file and the socket file; missing files are fine; never raises on OSError (logs a warning).
- `async def live_peers(self, *, exclude: str | None = None, instance: str | None = None) -> list[dict]`: reads every `*.json` entry (unreadable or invalid files are ignored with a debug log), drops `exclude` (by session_id) and entries of other instances when `instance` is given, probes every remaining socket concurrently with `asyncio.open_unix_connection` under `asyncio.wait_for(probe_timeout)` (closing the writer), keeps only the ones that accept, sorted by `(instance, name, session_id)`. The whole call is bounded by one probe timeout plus file reading, not by the number of entries. It never deletes anything.
- tests/test_peers_directory.py uses the `peers_dir` fixture (tests/conftest.py) and real listeners from `asyncio.start_unix_server` or `socket.socket(AF_UNIX)`.

Interfaces: `claude_code_wrapper.peers.{PeersError, PeerDirectory, instance_name, peer_session_name}`; on-disk entry `<dir>/directory/<session id>.json` = `{session_id, name, instance, address: "uds:<dir>/socks/<session id>.sock", cwd, started_at}` (contract `peer-directory-entry`).

Notes: the session id is unique per ccw (manager.py `_new_session_id`), so file names never collide across containers, unlike Claude's own `<pid>.json` registry (fact F5). The sweep is limited to this instance because a peer's claude restarting after a crash briefly refuses connections; deleting another instance's entry would hide a live session for good. Use `timeutil.now_iso` for timestamps where needed.

## Acceptance conditions

- AC1: prepare creates dir, socks and directory with mode 0700 and is idempotent [evidence: test:tests/test_peers_directory.py::test_prepare_creates_private_dirs]
- AC2: prepare refuses a group- or world-writable dir without sticky bit, and a dir owned by another uid, naming the path and the cc-socks fallback [evidence: test:tests/test_peers_directory.py::test_prepare_refuses_shared_or_foreign_dir]
- AC3: prepare refuses a group-writable non-sticky ancestor and accepts a sticky world-writable one [evidence: test:tests/test_peers_directory.py::test_prepare_vets_ancestors]
- AC4: publish writes the entry atomically with exactly the six keys and a uds address under socks [evidence: test:tests/test_peers_directory.py::test_publish_writes_entry_atomically]
- AC5: remove deletes the entry and the socket file and tolerates missing files [evidence: test:tests/test_peers_directory.py::test_remove_entry_and_socket]
- AC6: live_peers excludes the caller, hides refusing or missing sockets and filters by instance [evidence: test:tests/test_peers_directory.py::test_live_peers_filters_and_probes]
- AC7: live_peers probes concurrently, so ten entries that never answer finish in under three probe timeouts [evidence: test:tests/test_peers_directory.py::test_live_peers_probes_concurrently]
- AC8: prepare sweeps dead entries of its own instance and keeps other instances' entries [evidence: test:tests/test_peers_directory.py::test_prepare_sweeps_only_own_instance]
- AC9: instance_name falls back to the sanitized host name and peer_session_name joins instance and session name or id [evidence: test:tests/test_peers_directory.py::test_names]

## Files

- `src/claude_code_wrapper/peers.py`
- `tests/test_peers_directory.py`

## Interfaces

As in the Requirement's Interfaces paragraph: `peers.PeerDirectory`, `PeersError`, `instance_name`, `peer_session_name`; on-disk entry schema (contract `peer-directory-entry`).

## Expected outcome

A vetted, private peers dir with socks/ and directory/; entries are published and removed atomically; live_peers lists only peers whose socket accepts, concurrently and bounded.

## Steps

- Write the tests named in the acceptance conditions in `tests/test_peers_directory.py` first; run `uv run pytest tests/test_peers_directory.py -q` and see them fail for the missing behaviour (not an import typo).
- Implement the smallest change that makes them pass; run `uv run pytest tests/test_peers_directory.py -q` and expect all passed.
- Run the task verify: `uv run pytest tests/test_peers_directory.py -q && mise run lint`; expect pytest all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

See the Requirement's Notes paragraph.
