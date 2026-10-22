---
title: Peer http client
status: todo
repo: .
value: One ccw can deliver a message or idle notice to another over HTTP with clear errors
files: [src/claude_code_wrapper/peer_client.py, tests/test_peer_client.py]
verify: "uv run pytest tests/test_peer_client.py -q && mise run lint"
depends_on: [peer-directory-module]
owns: [peer-wire]
uses: [peer-directory-entry]
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

Wire models and the outbound HTTP client between ccw instances (design D1, D6). New file src/claude_code_wrapper/peer_client.py.

- Wire models (pydantic, extra fields forbidden):
  - `MessageBody {from_: str (alias "from"), to_session: str, message: str, msg_id: str, notify_when_idle: bool = False}`; `from_` must match `peers.ADDRESS_RE`, `to_session` the session id format, `msg_id` `msg_[0-9a-f]{16}`, `message` non-empty.
  - `NoticeBody {from_ (alias "from"), to_session, msg_id, state}` with `state` one of the terminal turn states.
  - `new_msg_id() -> str`.
- `class PeerError(Exception)` with `code` and `message`. Codes and the text each starts with: `unknown_peer` ("unknown peer"), `unreachable` ("peer unreachable"), `too_large` ("message too large"), `inbox_full` ("inbox full"), `unauthorized` ("peer rejected the token"), `invalid` ("invalid arguments"), `not_found` (the target session is unknown on the receiving side). `PeerError.status` maps receiving-side codes to HTTP: not_found 404, too_large 413, inbox_full 429, unauthorized 401.
- `class PeerClient(token: str, *, timeout: float = 10.0, transport: httpx.AsyncBaseTransport | None = None)`:
  - `async send_message(url, body: MessageBody) -> dict` POSTs `<url>/v1/peers/messages` with `Authorization: Bearer <token>` and the body by alias; 202 returns the JSON answer.
  - `async send_notice(url, body: NoticeBody) -> dict` POSTs `<url>/v1/peers/notices` the same way.
  - Answer mapping: 404 -> `not_found` surfaced as `unknown_peer`; 413 -> `too_large`; 429 -> `inbox_full`; 401 -> `unauthorized`; 422 -> `invalid`; a connect error, timeout or any other status -> `unreachable` with the url and the reason in the message. The token never appears in a `PeerError` message.
  - `async aclose()`.
- tests/test_peer_client.py: every mapping above through `httpx.MockTransport`, the Authorization header and alias `from` on the wire, model validation of bad addresses, session ids and msg ids, and `new_msg_id()` format.

Interfaces: `peer_client.MessageBody`, `NoticeBody`, `PeerError`, `PeerClient`, `new_msg_id` (the `peer-wire` part and the wire half of the cross-epic `peer-messages-endpoint` contract).

## Acceptance conditions

- AC1: the wire models validate addresses, session ids, msg ids and states and serialize from by alias [evidence: test:tests/test_peer_client.py::test_wire_models_validate]
- AC2: send_message and send_notice POST to the right path with the bearer peer token and return the 202 answer [evidence: test:tests/test_peer_client.py::test_send_posts_with_token]
- AC3: 404, 413, 429, 401 and 422 answers map to unknown_peer, too_large, inbox_full, unauthorized and invalid [evidence: test:tests/test_peer_client.py::test_status_mapping]
- AC4: connect errors, timeouts and other statuses map to unreachable naming the url, and no error message contains the token [evidence: test:tests/test_peer_client.py::test_unreachable_and_no_token_leak]
- AC5: new_msg_id returns msg_ plus 16 hex [evidence: test:tests/test_peer_client.py::test_new_msg_id]

## Files

- `src/claude_code_wrapper/peer_client.py`
- `tests/test_peer_client.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
