---
title: Peer messages endpoint
status: todo
repo: .
value: "Other ccw instances can join, heartbeat, leave and push messages and notices, only with a valid fresh signature"
files: [src/claude_code_wrapper/api/peers.py, src/claude_code_wrapper/api/app.py, tests/test_api_peers.py, README.md]
verify: "uv run pytest tests/test_api_peers.py tests/test_api_auth.py tests/test_readme_api.py -q && mise run lint"
depends_on: [peer-membership-hooks, peer-events-and-metrics, peer-http-client, peer-auth-signing, peers-config-section]
owns: [peer-messages-endpoint]
uses: [peer-wire, peer-auth, peers-config, peer-metrics]
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

# Peer messages endpoint

## Requirement

The receiving peer HTTP routes, authenticated by HMAC request signatures (design D5 routes, D6, D13).

- New src/claude_code_wrapper/api/peers.py:
  - `class PeerReceiver(Protocol)`: `async accept_message(body: MessageBody) -> dict`, `async accept_notice(body: NoticeBody) -> dict` (each `{msg_id, queued}` plus `duplicate: true` for a repeat), `async accept_join(view: MemberView) -> MemberView`, `async accept_heartbeat(view: MemberView) -> MemberView`, `async accept_leave(body: LeaveBody) -> None`; each may raise `PeerError` (`not_found`, `inbox_full`, `rate_limited`).
  - One `NonceCache(cfg.peers.max_clock_skew_seconds)` per app.
  - Five routes under `/v1/peers`: `POST messages`, `POST notices` (202), `POST join`, `POST heartbeat` (200 with the receiver's `MemberView` by alias), `POST leave` (204). Each, in this order: read the raw body with a cap of `peers.max_message_bytes + 4096` bytes, rejecting on `Content-Length` first and while streaming otherwise (413 `message_too_large`); `peer_auth.verify_request(cfg.peers.token, "POST", <path>, headers, raw, cache, skew_seconds=...)` over the raw bytes, any `PeerAuthError` is 401 `unauthorized` with a generic message (the reason only in a debug log), `NonceCacheFull` is 429 `rate_limited`; parse into the route's model (validation failure: the API's usual 422); for messages, a UTF-8 `message` over `max_message_bytes` is 413 `message_too_large`; then call the receiver at `request.app.state.manager.peers`. `PeerError` maps through `RECEIVER_STATUS` (not_found 404, inbox_full 429, rate_limited 429). An `Authorization` header never authenticates.
  - Metrics: `peer_received(kind, result)` for `unauthorized`, `too_large` and `not_found` decided here on messages and notices; `peer_membership("inbound_join" | "inbound_heartbeat" | "inbound_leave", "ok" | "failed")` for the membership routes.
- src/claude_code_wrapper/api/app.py: when `cfg.peers.enabled`, include the peers router at `/v1/peers` without the `/v1` `require_auth` dependency, and add `cfg.peers.token` to `manager.redactor`. Disabled, none of the five routes exists. Other routes unchanged.
- README.md: one `### \`POST /v1/peers/<route>\`` heading per route under `## API reference` (signature headers and canonical string, skew and nonce rules, body, answers, body cap, only mounted when enabled); `message_too_large`, `inbox_full` and `rate_limited` under `## Errors`.
- tests/test_api_peers.py, with a stub manager whose `peers` is a fake `PeerReceiver` and requests signed by `peer_auth.sign_request`: 202 for a signed message and notice, 200 with the view for join and heartbeat, 204 for leave; on every route 401 for no signature headers, a bad signature, a body changed after signing, a stale timestamp and a replayed nonce, with the receiver never called; bearer `server.auth_token` and bearer peer token without signature are 401, and a bearer peer token gets 401 on `GET /v1/sessions`; 413 for an oversize body by Content-Length and by streamed size; 413 oversize message; 404, 429 inbox_full and 429 rate_limited from `PeerError`; 422 malformed body; all five routes absent with peers disabled; README headings for all five when mounted.

Interfaces: `api.peers.PeerReceiver`, `api.peers.router` (the `peer-messages-endpoint` part and cross-epic contract).

## Acceptance conditions

- AC1: signed messages and notices get 202, join and heartbeat get 200 with the view, and leave gets 204 [evidence: test:tests/test_api_peers.py::test_accepts_signed_requests]
- AC2: on every route a missing or bad signature or a body changed after signing is 401 and the receiver is not called [evidence: test:tests/test_api_peers.py::test_bad_signature_401]
- AC3: a replayed nonce is 401 [evidence: test:tests/test_api_peers.py::test_replayed_nonce_401]
- AC4: a timestamp outside max_clock_skew_seconds is 401 [evidence: test:tests/test_api_peers.py::test_stale_timestamp_401]
- AC5: bearer tokens never authenticate a peer route and the peer token opens no other route [evidence: test:tests/test_api_peers.py::test_bearer_tokens_rejected]
- AC6: an oversize body is 413 before parsing, by Content-Length and by streamed size [evidence: test:tests/test_api_peers.py::test_body_cap_before_parse]
- AC7: an oversize message is 413, not_found is 404, inbox_full and rate_limited are 429 and a malformed body is 422 [evidence: test:tests/test_api_peers.py::test_error_statuses]
- AC8: with peers disabled none of the five routes is mounted [evidence: test:tests/test_api_peers.py::test_routes_absent_when_disabled]
- AC9: all five routes and the new error codes are documented in the README [evidence: test:tests/test_api_peers.py::test_peer_routes_documented]
- AC10: the existing API, auth and README suites stay green [evidence: command:uv run pytest tests/test_api_auth.py tests/test_api_sessions.py tests/test_readme_api.py -q]

## Files

- `src/claude_code_wrapper/api/peers.py`
- `src/claude_code_wrapper/api/app.py`
- `tests/test_api_peers.py`
- `README.md`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
