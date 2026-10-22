---
title: Peer http client
status: todo
repo: .
value: One ccw can deliver a signed message or notice to another over HTTP with clear errors and one safe retry
files: [src/claude_code_wrapper/peer_client.py, tests/test_peer_client.py]
verify: "uv run pytest tests/test_peer_client.py -q && mise run lint"
depends_on: [peer-directory-module, peer-auth-signing]
owns: [peer-wire]
uses: [peer-directory-entry, peer-auth]
model: sonnet
estimate: 60
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Peer http client

## Requirement

Wire models and the signing outbound HTTP client between ccw instances (design D1, D6, D13, D14). New file src/claude_code_wrapper/peer_client.py.

- Wire models (pydantic, extra fields forbidden):
  - `MessageBody {from_: str (alias "from"), to_session: str, message: str, msg_id: str, notify_when_idle: bool = False}`; `from_` must match `peers.ADDRESS_RE`, `to_session` the session id format, `msg_id` `msg_[0-9a-f]{16}`, `message` non-empty.
  - `NoticeBody {from_ (alias "from"), to_session, msg_id, status: "delivered" | "rejected" | "dropped", state: str | None = None, reason: str | None = None}`; `state` is a terminal turn state and required for `delivered`, null otherwise; `reason` at most 200 characters.
  - `new_msg_id() -> str`.
- `class PeerError(Exception)` with `code` and `message`. Client-side codes and the text each starts with: `unknown_peer` ("unknown peer"), `unreachable` ("peer unreachable"), `too_large` ("message too large"), `inbox_full` ("inbox full"), `rate_limited` ("rate limited"), `unauthorized` ("peer rejected the request"), `invalid` ("invalid arguments"). Receiving-side codes raised by the inbox and peer service: `not_found`, `inbox_full`, `rate_limited`. `RECEIVER_STATUS` maps them to HTTP for the endpoint: not_found 404, inbox_full 429, rate_limited 429.
- `class PeerClient(token: str, *, timeout: float = 10.0, transport: httpx.AsyncBaseTransport | None = None)`:
  - `async send_message(url, body: MessageBody) -> dict` POSTs `<url>/v1/peers/messages`; `async send_notice(url, body: NoticeBody) -> dict` POSTs `<url>/v1/peers/notices`. The body is serialized once (JSON by alias) and signed with `peer_auth.sign_request(token, "POST", path, body_bytes)`; no Authorization header is sent and the token is never on the wire. 202 returns the JSON answer.
  - Answer mapping (the error envelope is `{"error": {"code", "message"}}`): 404 -> `unknown_peer`; 413 -> `too_large`; 429 -> `inbox_full` or `rate_limited` from the envelope code (anything else on 429 -> `rate_limited`); 401 -> `unauthorized`; 422 -> `invalid`; any other status -> `unreachable`.
  - Retry: a connection-level failure (`httpx.ConnectError`, `ConnectTimeout`, `ReadTimeout`, `RemoteProtocolError`, `ReadError`, `WriteError`) is retried exactly once with the same body (same msg_id) and a fresh nonce and timestamp; a second failure is `unreachable` naming the url and the reason. An HTTP answer is never retried.
  - The token never appears in a `PeerError` message. `async aclose()`.
- tests/test_peer_client.py with `httpx.MockTransport` capturing every request: each mapping above; the signature headers verify with `peer_auth.verify_request`; `from` by alias; model validation of bad addresses, session ids, msg ids and notice status/state combinations; one retry on a connect error then success (both requests carry the same msg_id and different nonces); two connect errors give `unreachable`; a 500 is not retried; the token string appears in no captured request (url, headers, body) and no error message; `new_msg_id()` format.

Interfaces: `peer_client.MessageBody`, `NoticeBody`, `PeerError`, `RECEIVER_STATUS`, `PeerClient`, `new_msg_id` (the `peer-wire` part and the wire half of the cross-epic `peer-messages-endpoint` contract).

## Acceptance conditions

- AC1: the wire models validate addresses, session ids, msg ids and notice status and state, and serialize from by alias [evidence: test:tests/test_peer_client.py::test_wire_models_validate]
- AC2: send_message and send_notice POST to the right path with signature headers that verify and no Authorization header [evidence: test:tests/test_peer_client.py::test_requests_are_signed]
- AC3: 404, 413, 429 inbox_full, 429 rate_limited, 401 and 422 map to unknown_peer, too_large, inbox_full, rate_limited, unauthorized and invalid [evidence: test:tests/test_peer_client.py::test_status_mapping]
- AC4: a connection failure is retried once with the same msg_id and a fresh nonce, and an HTTP answer is never retried [evidence: test:tests/test_peer_client.py::test_retry_once_on_connection_failure]
- AC5: two connection failures map to unreachable naming the url [evidence: test:tests/test_peer_client.py::test_unreachable_after_retry]
- AC6: the token appears in no captured request and no error message [evidence: test:tests/test_peer_client.py::test_token_never_on_the_wire]
- AC7: new_msg_id returns msg_ plus 16 hex [evidence: test:tests/test_peer_client.py::test_new_msg_id]

## Files

- `src/claude_code_wrapper/peer_client.py`
- `tests/test_peer_client.py`

## Interfaces

See the Requirement's Interfaces paragraph; keep every name and signature stated there.

## Expected outcome

A typed wire format and a signing client that turns every outcome into one of a fixed set of PeerError codes and retries a connection failure once.

## Steps

- Write the tests named in the acceptance conditions in `tests/test_peer_client.py` first; run `uv run pytest tests/test_peer_client.py -q` and see them fail for the missing behaviour (not an import typo).
- Implement the smallest change that makes them pass; run `uv run pytest tests/test_peer_client.py -q` again and expect all passed.
- Run the task verify: `uv run pytest tests/test_peer_client.py -q && mise run lint`; expect all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

Use httpx (already a runtime dependency). Tests use httpx.MockTransport, never a network port.
