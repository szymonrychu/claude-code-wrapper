---
title: Peer member table
status: in-progress
repo: .
value: Each instance knows which peer instances are reachable and which sessions they run
files: [src/claude_code_wrapper/peers.py, tests/test_peers_membership.py]
verify: "uv run pytest tests/test_peers_membership.py -q && mise run lint"
depends_on: [peers-config-section]
owns: [peer-member-table]
uses: [peers-config]
model: sonnet
estimate: 60
actual: ""
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/cross-container-peer-messagi--peer-member-table
branch: cross-container-peer-messagi--peer-member-table
verified_head: ""
run: ""
provides: []
---

# Peer member table

## Requirement

The peer member table and addressing (design D5). New file src/claude_code_wrapper/peers.py: pure data structures, no I/O, no other module changes.

- `ADDRESS_RE`, `parse_address(address) -> tuple[str, str]` for `<instance>/<session id>` (instance `[A-Za-z0-9._-]{1,64}` not `.`, `..` or leading `.`; session id `ses_[0-9a-f]{12}`), a bad address raises `ValueError`; `format_address(instance, session_id)`.
- `resolve_instance_name(name)`: `name` when set, else `socket.gethostname()` sanitized to the instance charset, leading dots stripped, truncated to 64, `ccw` when nothing is left. `resolve_advertise_url(advertise_url, port)`: empty -> `http://<hostname>:<port>`; else `{port}` replaced.
- `@dataclass(frozen=True) PeerSession {session_id, name, cwd}` and `@dataclass Member {instance, url, sessions: tuple[PeerSession, ...], reachable: bool, missed: int, last_seen: float | None}`.
- `class MemberTable(self_instance, self_url, *, max_missed: int, capacity: int = 1024, clock=time.monotonic)`:
  - `add_contacts(urls)`: seeds and gossiped urls to contact, never our own url; contacts are not reachable until a direct exchange.
  - `targets() -> list[str]`: every contact and member url (not ours), for heartbeat rounds (the first round is the join).
  - `record_report(instance, url, sessions, members)`: a direct exchange with that member (its heartbeat to us, or its answer to ours): adds or refreshes it as reachable with exactly the sessions it reported, `missed = 0`, a known name with a new url takes the new url; `members` (`[{instance, url}]`) only add contacts. Ignores reports claiming our own instance name or url. Returns `"joined"` when the member became reachable, else None. Raises `TableFull` when a new member would exceed `capacity`.
  - `record_failure(url) -> str | None`: counts a miss for the member at that url; at `max_missed` consecutive misses it becomes unreachable (kept as a contact) and `"missed_heartbeats"` is returned; a contact that is not a static seed and has failed `10 x max_missed` consecutive rounds is forgotten (my call: dead gossiped urls do not accumulate). `add_contacts(urls, seed=True)` marks static seeds, which are never forgotten.
  - `remove(instance_or_url) -> bool`: a leave (a `leaving: true` heartbeat); the member becomes unreachable at once (url kept as a contact so a later heartbeat re-adds it).
  - `view(own_sessions) -> dict`: `{instance, url, sessions, members: [{instance, url} for reachable members]}` for heartbeat bodies and answers.
  - `reachable() -> list[Member]`, `sessions(exclude=None, instance=None) -> list[dict]` (`{address, instance, session_id, name, cwd}` of reachable members, sorted by address, excluding an address, filtered by instance), `lookup(address) -> str | None` (the url of the reachable member owning that address, or None).
- tests/test_peers_membership.py: addresses and name/url resolution; contacts are not reachable; a report makes a member reachable with its sessions and replaces them on the next report; gossip only adds contacts; `max_missed` misses make it unreachable and a later report re-adds it with `"joined"`; a leave makes it unreachable at once; a gossiped contact failing 10 x max_missed rounds is forgotten while a seed is kept; a known name with a new url; our own name and url ignored; capacity raises `TableFull`; `sessions()` exclude and filter; `lookup` for reachable, unreachable and unknown addresses; `view()` lists only reachable members.

Interfaces: `peers.MemberTable`, `Member`, `PeerSession`, `TableFull`, `parse_address`, `format_address`, `resolve_instance_name`, `resolve_advertise_url` (the `peer-member-table` part and cross-epic contract).

## Acceptance conditions

- AC1: addresses parse and format as instance/session id, bad ones including dot names raise ValueError, and name and url resolve [evidence: test:tests/test_peers_membership.py::test_addresses_and_resolution]
- AC2: a direct report makes a member reachable with exactly its reported sessions while gossip only adds contacts [evidence: test:tests/test_peers_membership.py::test_reports_and_gossip]
- AC3: max_missed consecutive misses make a member unreachable and a later report re-adds it [evidence: test:tests/test_peers_membership.py::test_missed_heartbeats_and_readd]
- AC4: a leave makes a member unreachable at once, and dead gossiped contacts are forgotten while seeds are kept [evidence: test:tests/test_peers_membership.py::test_leave_removes]
- AC5: a known name with a new url takes it, our own name and url are ignored, and capacity raises TableFull [evidence: test:tests/test_peers_membership.py::test_identity_rules_and_capacity]
- AC6: sessions, lookup and view cover reachable members only, with exclude and instance filter [evidence: test:tests/test_peers_membership.py::test_sessions_lookup_view]

## Files

- `src/claude_code_wrapper/peers.py`
- `tests/test_peers_membership.py`


## Interfaces

See the Requirement's Interfaces paragraph; keep every name and signature stated there.

## Expected outcome

A bounded member table that knows which instances are reachable, with which sessions, and whom to contact next.

## Steps

- Write the tests named in the acceptance conditions in `tests/test_peers_membership.py` first; run `uv run pytest tests/test_peers_membership.py -q` and see them fail for the missing behaviour (not an import typo).
- Implement the smallest change that makes them pass; run `uv run pytest tests/test_peers_membership.py -q` again and expect all passed.
- Run the task verify: `uv run pytest tests/test_peers_membership.py -q && mise run lint`; expect all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

Pure and synchronous; all I/O lives in peer_membership.py.
