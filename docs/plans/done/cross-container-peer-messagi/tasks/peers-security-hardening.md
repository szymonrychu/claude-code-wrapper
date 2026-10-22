---
title: Peers security hardening
status: done
repo: .
value: "Closes the epic security review findings: bounded gossip contacts, capped response reads, no 500 on malformed timestamps, per-session total rate limit, bounded remote text"
files: [src/claude_code_wrapper/peers.py, src/claude_code_wrapper/peer_membership.py, src/claude_code_wrapper/peer_client.py, src/claude_code_wrapper/peer_auth.py, src/claude_code_wrapper/peer_inbox.py, src/claude_code_wrapper/config.py, README.md, tests/test_peers_hardening.py]
verify: "uv run pytest tests/test_peers_hardening.py tests/test_peer_auth.py tests/test_peer_inbox.py tests/test_peer_client.py tests/test_peers_config.py tests/test_readme_config.py -q && mise run lint"
depends_on: [fake-claude-peer-harness, manager-peers-lifecycle, peer-auth-signing, peer-events-and-metrics, peer-http-client, peer-inbox-delivery, peer-inbox-outcomes, peer-member-table, peer-membership-hooks, peer-membership-service, peer-messages-endpoint, peer-service-core, peer-session-bootstrap, peers-compose-example, peers-config-section, peers-join-leave-order, peers-mcp-tools, peers-readme-guide, peers-two-container-test, peers-two-instance-e2e]
owns: []
uses: []
model: sonnet
estimate: 60
actual: ""
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/cross-container-peer-messagi--peers-security-hardening
branch: cross-container-peer-messagi--peers-security-hardening
verified_head: 62f99c7d907e485cae7fcc7be96d64ce98f5a0c7
run: cross-container-peer-messagi
provides: []
acs_for_audit: []
process_gaps: []
---

# Peers security hardening

## Requirement

Epic security review hardening (verified findings on main):
- peers.py `add_contacts` (and the `_contacts.setdefault` in record paths, peer_membership.py:140 gossip feed): cap the contact set at the member table capacity; once full, unknown gossip urls are ignored (seeds and already known urls always kept). Contacts learned only from gossip that never answered a heartbeat are evicted first when room is needed, and are forgotten after max_missed_heartbeats failed attempts.
- peer_client.py heartbeat/message/notice answers: read the response body streamed with a hard cap (e.g. 1 MiB or a named constant derived from max_message_bytes); over the cap is an `unreachable` error, never an unbounded read.
- peer_auth.py verify: bound the timestamp header length (at most 16 digits) in the malformed check so an absurd value is 401 malformed, never a 500 / ValueError.
- peer_inbox.py: add a per-session global inbound message rate limit (all senders together, config peers.max_messages_per_minute_total, default 120, documented in README config rows) in addition to the per-sender one, and bound the `_rate` map (drop empty deques; cap distinct sender keys, oldest evicted).
- peer_client.py `ViewSession.name` max_length 200 and `cwd` max_length 1024 (and any other remote free-text field reaching the peers tool result), rejecting oversize views as malformed; control characters stripped from name/cwd before they reach the tool result.

## Acceptance conditions

- AC1: gossip advertising more urls than the table capacity leaves the contact set at or below capacity, seeds kept [evidence: test:tests/test_peers_hardening.py::test_gossip_contacts_bounded]
- AC2: a heartbeat answer larger than the cap gives an unreachable error without reading it all [evidence: test:tests/test_peers_hardening.py::test_oversize_answer_unreachable]
- AC3: a request with a 5000-digit timestamp gets 401 malformed [evidence: test:tests/test_peers_hardening.py::test_huge_timestamp_is_malformed]
- AC4: messages from rotating from addresses beyond max_messages_per_minute_total get rate_limited; the _rate map stays bounded [evidence: test:tests/test_peers_hardening.py::test_total_rate_limit_and_bounded_rate_map]
- AC5: an oversize name or cwd in a peer view is rejected; control characters never reach the peers tool result [evidence: test:tests/test_peers_hardening.py::test_remote_text_bounded_and_clean]
- AC6: README documents peers.max_messages_per_minute_total and test_every_config_key_documented passes [evidence: test:tests/test_readme_config.py::test_every_config_key_documented]

## Files

- `src/claude_code_wrapper/peers.py`
- `src/claude_code_wrapper/peer_membership.py`
- `src/claude_code_wrapper/peer_client.py`
- `src/claude_code_wrapper/peer_auth.py`
- `src/claude_code_wrapper/peer_inbox.py`
- `src/claude_code_wrapper/config.py`
- `README.md`
- `tests/test_peers_hardening.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

Every security review finding (medium and low) is closed with a pinning test; the peers suites and lint stay green.

What is true and observable when the task is done.

## Steps

1. Write tests/test_peers_hardening.py with one failing test per AC; run it and see each fail.
2. Implement each bound in the listed module.
3. Add the README config row; run the verify command.

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
