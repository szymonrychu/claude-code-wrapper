---
title: Peer messages endpoint
status: todo
repo: .
value: "Other ccw instances can push messages and notices in, only with a valid fresh signature"
files: [src/claude_code_wrapper/api/peers.py, src/claude_code_wrapper/api/app.py, tests/test_api_peers.py, README.md]
verify: "uv run pytest tests/test_api_peers.py tests/test_api_auth.py tests/test_readme_api.py -q && mise run lint"
depends_on: [peer-events-and-metrics, peer-http-client, peer-auth-signing, peers-config-section]
owns: [peer-messages-endpoint]
uses: [peer-wire, peer-auth, peers-config, peer-metrics]
model: sonnet
estimate: 60
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

The receiving HTTP routes for peer messages and notices, authenticated by HMAC request signatures (design D6, D13).

- New src/claude_code_wrapper/api/peers.py:
  - `class PeerReceiver(Protocol)`: `async accept_message(body: MessageBody) -> dict` and `async accept_notice(body: NoticeBody) -> dict`, each returning `{msg_id, queued}` (plus `duplicate: true` for a repeat) or raising `PeerError` (`not_found`, `inbox_full`, `rate_limited`).
  - One `NonceCache(cfg.peers.max_clock_skew_seconds)` per app.
  - `POST /v1/peers/messages` and `POST /v1/peers/notices`, in this order: read the raw body with a cap of `peers.max_message_bytes + 4096` bytes, rejecting on `Content-Length` first and while streaming otherwise (413 `message_too_large`); `peer_auth.verify_request(cfg.peers.token, "POST", <path>, headers, raw, cache, skew_seconds=...)` over the raw bytes, any `PeerAuthError` is 401 `unauthorized` with a generic message (the reason only in a debug log), `NonceCacheFull` is 429 `rate_limited`; then parse into `MessageBody` / `NoticeBody` (a validation failure is the API's usual 422); a message whose UTF-8 `message` exceeds `max_message_bytes` is 413 `message_too_large`; then `await request.app.state.manager.peers.accept_message(body)` (or `accept_notice`). `PeerError` maps through `RECEIVER_STATUS` with codes `not_found` (404), `inbox_full` (429), `rate_limited` (429). Success is 202 with the receiver's JSON. An `Authorization` header is ignored: a bearer `server.auth_token` or the raw peer token does not authenticate.
  - Metrics: `peer_received(kind, result)` for the outcomes decided here (`unauthorized`, `too_large`, `not_found`, and `rate_limited` from a full nonce cache) when metrics are configured.
- src/claude_code_wrapper/api/app.py: when `cfg.peers.enabled`, include the peers router at `/v1/peers` without the `/v1` `require_auth` dependency, and add `cfg.peers.token` to `manager.redactor`. Disabled, neither route exists. `server.auth_token` checks on every other route are unchanged.
- README.md: `### \`POST /v1/peers/messages\`` and `### \`POST /v1/peers/notices\`` under `## API reference` (the three signature headers and the canonical string, the skew window and nonce rule, body, answers, the body cap, only mounted when enabled); `message_too_large`, `inbox_full` and `rate_limited` under `## Errors` (tests/test_readme_api.py scrapes codes from api/*.py).
- tests/test_api_peers.py, with a stub manager whose `peers` is a fake `PeerReceiver` and requests signed by `peer_auth.sign_request`: 202 for a valid message and notice; 401 for no signature headers, a bad signature, a body changed after signing, a timestamp outside the window and a replayed nonce, with the receiver never called; 401 for `Authorization: Bearer <server.auth_token>` and `Bearer <peer token>` without signature headers; a bearer peer token gets 401 on `GET /v1/sessions`; 413 for an oversize body by Content-Length and by streamed size; 413 for an oversize message; 404, 429 inbox_full and 429 rate_limited from `PeerError`; 422 for a malformed body; both routes absent with peers disabled; the README documents both routes when they are mounted (build the app with peers enabled and compare route headings like `test_every_public_route_documented`).

Interfaces: `api.peers.PeerReceiver`, `api.peers.router` (the `peer-messages-endpoint` part and cross-epic contract: routes, signing, body and status codes).

## Acceptance conditions

- AC1: a correctly signed message and notice get 202 with the receiver answer [evidence: test:tests/test_api_peers.py::test_accepts_signed_requests]
- AC2: a missing or bad signature or a body changed after signing is 401 and the receiver is not called [evidence: test:tests/test_api_peers.py::test_bad_signature_401]
- AC3: a replayed nonce is 401 [evidence: test:tests/test_api_peers.py::test_replayed_nonce_401]
- AC4: a timestamp outside max_clock_skew_seconds is 401 [evidence: test:tests/test_api_peers.py::test_stale_timestamp_401]
- AC5: bearer server.auth_token or bearer peer token never authenticates a peer route, and the peer token opens no other route [evidence: test:tests/test_api_peers.py::test_bearer_tokens_rejected]
- AC6: an oversize body is 413 before parsing, by Content-Length and by streamed size [evidence: test:tests/test_api_peers.py::test_body_cap_before_parse]
- AC7: an oversize message is 413, not_found is 404, inbox_full and rate_limited are 429 and a malformed body is 422 [evidence: test:tests/test_api_peers.py::test_error_statuses]
- AC8: with peers disabled neither route is mounted [evidence: test:tests/test_api_peers.py::test_routes_absent_when_disabled]
- AC9: both routes and the new error codes are documented in the README [evidence: test:tests/test_api_peers.py::test_peer_routes_documented]
- AC10: the existing API, auth and README suites stay green [evidence: command:uv run pytest tests/test_api_auth.py tests/test_api_sessions.py tests/test_readme_api.py -q]

## Files

- `src/claude_code_wrapper/api/peers.py`
- `src/claude_code_wrapper/api/app.py`
- `tests/test_api_peers.py`
- `README.md`

## Interfaces

See the Requirement's Interfaces paragraph; keep every name and signature stated there.

## Expected outcome

With peers enabled the two peer routes accept only correctly signed, fresh, unreplayed requests, cap bodies before parsing and map receiver errors; with peers disabled they do not exist.

## Steps

- Write the tests named in the acceptance conditions in `tests/test_api_peers.py` first; run `uv run pytest tests/test_api_peers.py -q` and see them fail for the missing behaviour (not an import typo).
- Implement the smallest change that makes them pass; run `uv run pytest tests/test_api_peers.py -q` again and expect all passed.
- Run the task verify: `uv run pytest tests/test_api_peers.py tests/test_api_auth.py tests/test_readme_api.py -q && mise run lint`; expect all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

The routes call request.app.state.manager.peers (a PeerReceiver), which manager-peers-lifecycle provides; tests use a stub. Third README-editing task.
