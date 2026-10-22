---
title: Peer messages endpoint
status: todo
repo: .
value: "Other ccw instances can push messages and idle notices in, and only with the peer token"
files: [src/claude_code_wrapper/api/peers.py, src/claude_code_wrapper/api/app.py, tests/test_api_peers.py, README.md]
verify: "uv run pytest tests/test_api_peers.py tests/test_api_auth.py tests/test_readme_api.py -q && mise run lint"
depends_on: [peer-http-client, peers-config-section, peer-events-and-metrics]
owns: [peer-messages-endpoint]
uses: [peer-wire, peers-config, peer-metrics]
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

The receiving HTTP routes for peer messages and idle notices (design D6).

- New src/claude_code_wrapper/api/peers.py:
  - `class PeerReceiver(Protocol)`: `async accept_message(body: MessageBody) -> dict` and `async accept_notice(body: NoticeBody) -> dict`, each returning `{msg_id, queued}` (plus `duplicate: true` for a repeat) or raising `PeerError` (`not_found`, `inbox_full`).
  - `require_peer_auth` dependency: `Authorization: Bearer <cfg.peers.token>` compared with `hmac.compare_digest`; a missing or wrong token, including `server.auth_token`, is 401 `unauthorized` with `WWW-Authenticate: Bearer`.
  - `POST /v1/peers/messages` and `POST /v1/peers/notices`: the body is read with a cap of `peers.max_message_bytes + 4096` bytes, rejecting on `Content-Length` first and while streaming otherwise, before any JSON parse (413 `message_too_large`); then parsed into `MessageBody` / `NoticeBody` (a validation failure is the API's usual 422); a message whose UTF-8 `message` exceeds `max_message_bytes` is 413 `message_too_large`; then `await request.app.state.manager.peers.accept_message(body)` (or `accept_notice`). `PeerError` maps to its status with code `not_found` (404), `inbox_full` (429), `message_too_large` (413). Success is 202 with the receiver's JSON.
  - Metrics: `peer_received(kind, result)` for the outcomes decided here (`unauthorized`, `too_large`, `not_found`) when metrics are configured.
- src/claude_code_wrapper/api/app.py: when `cfg.peers.enabled`, include the peers router at `/v1/peers` without the `/v1` `require_auth` dependency, and add `cfg.peers.token` to `manager.redactor`. Disabled, neither route exists (404 as any unknown path). `server.auth_token` checks on every other route are unchanged, so the peer token opens nothing else.
- README.md: `### \`POST /v1/peers/messages\`` and `### \`POST /v1/peers/notices\`` under `## API reference` (auth, body, answers, the body cap, only mounted when enabled); `message_too_large` and `inbox_full` under `## Errors` (tests/test_readme_api.py scrapes codes from api/*.py).
- tests/test_api_peers.py, with a stub manager whose `peers` is a fake `PeerReceiver`: 202 for a valid message and notice; 401 for no token, a wrong token and `server.auth_token`; the peer token gets 401 on `GET /v1/sessions`; 413 for an oversize body by Content-Length and by streamed size with the receiver never called; 413 for an oversize message; 404 and 429 from `PeerError`; 422 for a malformed body; both routes absent with peers disabled; the README documents both routes when they are mounted (build the app with peers enabled and compare route headings like `test_every_public_route_documented`).

Interfaces: `api.peers.PeerReceiver`, `api.peers.router` (the `peer-messages-endpoint` part and cross-epic contract: routes, auth, body and status codes).

## Acceptance conditions

- AC1: a valid message and a valid notice with the peer token get 202 with the receiver answer [evidence: test:tests/test_api_peers.py::test_accepts_with_peer_token]
- AC2: no token, a wrong token and server.auth_token get 401 on both peer routes [evidence: test:tests/test_api_peers.py::test_peer_routes_reject_other_tokens]
- AC3: the peer token is rejected on the regular v1 routes [evidence: test:tests/test_api_peers.py::test_peer_token_opens_nothing_else]
- AC4: an oversize body is 413 before parsing, by Content-Length and by streamed size, and the receiver is not called [evidence: test:tests/test_api_peers.py::test_body_cap_before_parse]
- AC5: an oversize message is 413, not_found is 404, inbox_full is 429 and a malformed body is 422 [evidence: test:tests/test_api_peers.py::test_error_statuses]
- AC6: with peers disabled neither route is mounted [evidence: test:tests/test_api_peers.py::test_routes_absent_when_disabled]
- AC7: both routes and the new error codes are documented in the README [evidence: test:tests/test_api_peers.py::test_peer_routes_documented]
- AC8: the existing API, auth and README suites stay green [evidence: command:uv run pytest tests/test_api_auth.py tests/test_api_sessions.py tests/test_readme_api.py -q]

## Files

- `src/claude_code_wrapper/api/peers.py`
- `src/claude_code_wrapper/api/app.py`
- `tests/test_api_peers.py`
- `README.md`

## Interfaces

See the Requirement's Interfaces paragraph; keep every name and signature stated there.

## Expected outcome

With peers enabled the two peer routes accept only the peer token, cap bodies before parsing and map receiver errors to 404, 413 and 429; with peers disabled they do not exist.

## Steps

- Write the tests named in the acceptance conditions in `tests/test_api_peers.py` first; run `uv run pytest tests/test_api_peers.py -q` and see them fail for the missing behaviour (not an import typo).
- Implement the smallest change that makes them pass; run `uv run pytest tests/test_api_peers.py -q` and expect all passed.
- Run the task verify: `uv run pytest tests/test_api_peers.py tests/test_api_auth.py tests/test_readme_api.py -q && mise run lint`; expect all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

The routes call request.app.state.manager.peers (a PeerReceiver), which manager-peers-lifecycle provides; tests use a stub.
