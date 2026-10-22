---
title: Peer directory module
status: todo
repo: .
value: Every ccw instance sharing a volume can see which signed peer sessions are alive and where to reach them
files: [src/claude_code_wrapper/peers.py, tests/test_peers_directory.py]
verify: "uv run pytest tests/test_peers_directory.py -q && mise run lint"
depends_on: [peers-config-section, peer-auth-signing]
owns: [peer-directory-entry]
uses: [peers-config, peer-auth]
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

# Peer directory module

## Requirement

The shared peer directory module (design D5, D13 entries). New file src/claude_code_wrapper/peers.py; no other module changes.

- `ADDRESS_RE` and `parse_address(address) -> tuple[str, str]` for `<instance>/<session id>` (instance `[A-Za-z0-9._-]{1,64}` not `.`, `..` or leading `.`; session id `ses_[0-9a-f]{12}`); a bad address raises `ValueError`. `format_address(instance, session_id)`.
- `resolve_instance_name(name: str) -> str`: `name` when set, else `socket.gethostname()` with characters outside `[A-Za-z0-9._-]` replaced by `-`, leading dots stripped, truncated to 64, `ccw` when nothing is left.
- `resolve_advertise_url(advertise_url: str, port: int) -> str`: empty -> `http://<socket.gethostname()>:<port>`; otherwise `{port}` replaced by `port`.
- `@dataclass(frozen=True) PeerEntry {instance, session_id, name, cwd, url, started_at, heartbeat_at}` with `address`, `to_fields()` and `from_fields()` (unknown keys ignored, missing or wrong-typed keys raise `ValueError`). Times are ISO 8601 UTC with millisecond precision.
- `class PeersError(Exception)`; `DIRECTORY_IO_TIMEOUT = 5.0`.
- `class PeerDirectory(directory, instance, url, token, *, heartbeat_seconds, stale_seconds, clock=time.time, io_timeout=DIRECTORY_IO_TIMEOUT)`. Every public method is async and runs its file I/O in `asyncio.to_thread` under `asyncio.wait_for(io_timeout)`:
  - `prepare()`: creates `<directory>/<instance>/` (parents too); deletes every entry in it, logging one warning naming the count when any was live (names must be unique); a failure raises `PeersError`.
  - `publish(session_id, *, name, cwd, started_at) -> PeerEntry`: writes `<directory>/<instance>/<session id>.json` atomically (temp file in the same dir, fsync, `os.replace`) with `heartbeat_at` = now and `sig` = `peer_auth.entry_signature(token, fields)`; remembers it as ours; a timeout or OSError raises `PeersError`.
  - `heartbeat()`: rewrites `heartbeat_at` and `sig` of every entry we published (a deleted file is recreated), then garbage-collects: under every instance dir, deletes entries whose `heartbeat_at` is older than `10 x stale_seconds` (signed or not; unparsable files older than that by mtime too) and removes empty instance dirs other than ours.
  - `remove(session_id)`: deletes our file (missing is fine) and forgets it; errors are logged, not raised.
  - `live(*, exclude=None, instance=None) -> list[PeerEntry]`: scans `<directory>/*/*.json`, skips temp, unreadable, malformed, unsigned and badly signed files (verified with `peer_auth.verify_entry`), keeps `now - heartbeat_at <= stale_seconds`, drops `exclude`, filters by instance, sorted by address; on a timeout or OSError returns `[]` and logs.
  - `lookup(address) -> PeerEntry | None`: the live entry for that address.
  - `async run_heartbeat()`: `heartbeat()` every `heartbeat_seconds` until cancelled; a timeout or OSError is logged and the loop goes on.
- No file in the directory contains the token.
- tests/test_peers_directory.py with a tmp directory and an injected clock: two PeerDirectory objects for different instances on one directory see each other; stale entries hidden; an entry with a forged or missing `sig` (written by hand or signed with another token) ignored by `live()` and `lookup()`; prepare deletes all of our own dir and warns when one looked live, never touching another instance's dir; heartbeat GC deletes entries older than 10 x stale under any name and leaves younger ones; an atomic write leaves no temp file; malformed files skipped; `.`/`..`/leading-dot names rejected by `parse_address`; a blocking fake filesystem (monkeypatched `os.replace`/`os.scandir` that sleeps past a short `io_timeout`) makes `live()` return `[]` and `publish` raise `PeersError` within the timeout while another coroutine on the loop keeps running; the token string absent from every written file.

Interfaces: `peers.PeerDirectory`, `PeerEntry`, `PeersError`, `parse_address`, `format_address`, `resolve_instance_name`, `resolve_advertise_url`, `DIRECTORY_IO_TIMEOUT` (the `peer-directory-entry` contract: the file layout, keys and signature every ccw instance sharing the directory reads).

## Acceptance conditions

- AC1: addresses parse and format as instance/session id, and bad ones including dot names raise ValueError [evidence: test:tests/test_peers_directory.py::test_address_parse_and_format]
- AC2: instance name and advertise url resolve from config, hostname and the bound port including the {port} placeholder [evidence: test:tests/test_peers_directory.py::test_resolve_name_and_url]
- AC3: publish writes a signed entry atomically under the instance directory and remove deletes it [evidence: test:tests/test_peers_directory.py::test_publish_and_remove]
- AC4: live lists signed entries of every instance, hides stale and malformed ones, excludes the caller and filters by instance [evidence: test:tests/test_peers_directory.py::test_live_listing]
- AC5: a forged, foreign-token or unsigned entry is ignored by live and lookup [evidence: test:tests/test_peers_directory.py::test_forged_entry_ignored]
- AC6: prepare deletes every entry under our own name with a warning when one looked live and leaves other instances alone [evidence: test:tests/test_peers_directory.py::test_prepare_sweeps_own_dir]
- AC7: heartbeat refreshes our entries and deletes entries older than ten times stale_seconds under any name [evidence: test:tests/test_peers_directory.py::test_heartbeat_refresh_and_gc]
- AC8: a stalled filesystem makes live return an empty list and publish raise PeersError within the timeout without blocking the loop [evidence: test:tests/test_peers_directory.py::test_stalled_filesystem_degrades]
- AC9: no written file contains the token [evidence: test:tests/test_peers_directory.py::test_token_not_in_files]

## Files

- `src/claude_code_wrapper/peers.py`
- `tests/test_peers_directory.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
