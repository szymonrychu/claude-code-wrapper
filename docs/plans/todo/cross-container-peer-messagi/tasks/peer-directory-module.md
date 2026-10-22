---
title: Peer directory module
status: todo
repo: .
value: Every ccw instance sharing a volume can see which peer sessions are alive and where to reach them
files: [src/claude_code_wrapper/peers.py, tests/test_peers_directory.py]
verify: "uv run pytest tests/test_peers_directory.py -q && mise run lint"
depends_on: [peers-config-section]
owns: [peer-directory-entry]
uses: [peers-config]
model: sonnet
estimate: 75
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

The shared peer directory module (design D5). New file src/claude_code_wrapper/peers.py; no other module changes.

- `ADDRESS_RE` and `parse_address(address) -> tuple[str, str]` for `<instance>/<session id>` (instance `[A-Za-z0-9._-]{1,64}`, session id `ses_[0-9a-f]{12}`); a bad address raises `ValueError`. `format_address(instance, session_id)`.
- `resolve_instance_name(name: str) -> str`: `name` when set, else `socket.gethostname()` with characters outside `[A-Za-z0-9._-]` replaced by `-`, truncated to 64.
- `resolve_advertise_url(advertise_url: str, port: int) -> str`: empty -> `http://<socket.gethostname()>:<port>`; otherwise `{port}` replaced by `port`.
- `@dataclass(frozen=True) PeerEntry {instance, session_id, name, cwd, url, started_at, heartbeat_at}` with `address` property, `to_json()` and `from_json()` (unknown keys ignored, missing or wrong-typed keys raise `ValueError`).
- `class PeersError(Exception)` for startup refusals.
- `class PeerDirectory(directory, instance, url, *, heartbeat_seconds, stale_seconds, clock=time.time)`:
  - `prepare()`: creates `<directory>/<instance>/` (parents too) if missing; removes every entry there whose `url` equals ours and every stale entry; raises `PeersError("name <instance> is in use by a live instance at <url>")` when a live entry with a different `url` remains. Never reads or writes another instance's subdirectory for removal.
  - `publish(session_id, *, name, cwd, started_at) -> PeerEntry`: writes `<directory>/<instance>/<session id>.json` atomically (temp file in the same dir, fsync, `os.replace`), `heartbeat_at` = now, and remembers it as ours.
  - `heartbeat() -> None`: rewrites `heartbeat_at` of every entry we published (a deleted file is recreated).
  - `remove(session_id)`: deletes our file (missing is fine) and forgets it.
  - `live(*, exclude: str | None = None, instance: str | None = None) -> list[PeerEntry]`: scans `<directory>/*/*.json`, skips unreadable, malformed and temp files, keeps entries with `now - heartbeat_at <= stale_seconds`, drops the `exclude` address, filters by instance, sorted by address.
  - `lookup(address) -> PeerEntry | None`: the live entry for that address.
  - `async run_heartbeat()`: calls `heartbeat()` every `heartbeat_seconds` until cancelled; an OSError is logged and the loop goes on.
- Times are ISO 8601 UTC strings (use `timeutil` if it fits).
- tests/test_peers_directory.py covers every function above with a tmp directory and an injected clock, including two PeerDirectory objects for different instances on one directory, a stale entry hidden by `live()`, prepare removing our own leftovers but not a live foreign-instance entry, the name-clash refusal, an atomic write leaving no temp file and a malformed file being skipped.

Interfaces: `peers.PeerDirectory`, `peers.PeerEntry`, `peers.PeersError`, `peers.parse_address`, `peers.format_address`, `peers.resolve_instance_name`, `peers.resolve_advertise_url` (the `peer-directory-entry` contract: the file layout and JSON keys every ccw instance sharing the directory reads).

## Acceptance conditions

- AC1: addresses parse and format as instance/session id and bad ones raise ValueError [evidence: test:tests/test_peers_directory.py::test_address_parse_and_format]
- AC2: instance name and advertise url resolve from config, hostname and the bound port including the {port} placeholder [evidence: test:tests/test_peers_directory.py::test_resolve_name_and_url]
- AC3: publish writes the seven-key entry atomically under the instance directory and remove deletes it [evidence: test:tests/test_peers_directory.py::test_publish_and_remove]
- AC4: heartbeat refreshes heartbeat_at and recreates a deleted entry [evidence: test:tests/test_peers_directory.py::test_heartbeat_refreshes]
- AC5: live lists entries of every instance, hides stale and malformed ones, excludes the caller and filters by instance [evidence: test:tests/test_peers_directory.py::test_live_listing]
- AC6: prepare removes our leftovers and stale entries, keeps other instances, and refuses a live name clash with PeersError [evidence: test:tests/test_peers_directory.py::test_prepare_sweeps_and_refuses_clash]
- AC7: run_heartbeat keeps going after an OSError and stops on cancel [evidence: test:tests/test_peers_directory.py::test_run_heartbeat_loop]

## Files

- `src/claude_code_wrapper/peers.py`
- `tests/test_peers_directory.py`

## Interfaces

See the Requirement's Interfaces paragraph; keep every name and signature stated there.

## Expected outcome

Several PeerDirectory objects on one directory see each other's live sessions, hide stale ones and never delete another instance's entries.

## Steps

- Write the tests named in the acceptance conditions in `tests/test_peers_directory.py` first; run `uv run pytest tests/test_peers_directory.py -q` and see them fail for the missing behaviour (not an import typo).
- Implement the smallest change that makes them pass; run `uv run pytest tests/test_peers_directory.py -q` and expect all passed.
- Run the task verify: `uv run pytest tests/test_peers_directory.py -q && mise run lint`; expect all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

The file layout is a cross-instance contract (peer-directory-entry): keep the key names exactly. No socket probes.
