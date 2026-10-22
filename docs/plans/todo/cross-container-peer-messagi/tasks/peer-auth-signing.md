---
title: Peer auth signing
status: todo
repo: .
value: The peer token authenticates requests and directory entries without ever leaving the process
files: [src/claude_code_wrapper/peer_auth.py, tests/test_peer_auth.py]
verify: "uv run pytest tests/test_peer_auth.py -q && mise run lint"
depends_on: []
owns: [peer-auth]
uses: []
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

# Peer auth signing

## Requirement

HMAC-SHA256 signing of peer requests and directory entries (design D13). New file src/claude_code_wrapper/peer_auth.py; pure functions plus one cache, no I/O, no other module changes.

- Header names `HEADER_TIMESTAMP = "X-CCW-Peer-Timestamp"`, `HEADER_NONCE = "X-CCW-Peer-Nonce"`, `HEADER_SIGNATURE = "X-CCW-Peer-Signature"`.
- `canonical_request(method, path, timestamp_ms, nonce, body: bytes) -> bytes`: `<METHOD upper>\n<path>\n<timestamp_ms>\n<nonce>\n<sha256 hex of body>`.
- `sign_request(token, method, path, body, *, now_ms=None, nonce=None) -> dict[str, str]`: the three headers; timestamp is unix milliseconds, nonce 32 lowercase hex from `secrets`.
- `class NonceCache(window_seconds: float, *, capacity: int = 65536, clock=time.monotonic)`: `add(nonce) -> bool` (False when already seen within the retention of `2 x window_seconds`); expired nonces are evicted first; when still at capacity `add` raises `NonceCacheFull` (never evicts a live nonce).
- `verify_request(token, method, path, headers, body, cache, *, skew_seconds, now_ms=None) -> None`: raises `PeerAuthError(reason)` with reason `missing`, `malformed`, `bad_signature`, `stale` (timestamp outside +-skew) or `replayed`; the signature is checked with `hmac.compare_digest` before the nonce is recorded, so a forged request cannot burn a nonce; `NonceCacheFull` propagates for the caller to answer 429.
- `entry_signature(token, fields: Mapping[str, object]) -> str`: hex HMAC-SHA256 over `json.dumps(fields, sort_keys=True, separators=(",", ":"))` of every field except `sig`. `verify_entry(token, fields) -> bool`: False for a missing, non-string or wrong `sig`, constant-time compare.
- No function puts the token into an exception message, a return value or a log line.
- tests/test_peer_auth.py: sign then verify round trip; tampered method, path, body, timestamp or nonce each give bad_signature; missing and malformed headers; a timestamp just inside and just outside the skew window; a replayed nonce; a forged signature does not consume the nonce; NonceCache expiry and the capacity error; entry signature round trip, a changed field or a missing sig fails; the token string never appears in any header value, exception text or repr.

Interfaces: `peer_auth.sign_request`, `verify_request`, `NonceCache`, `NonceCacheFull`, `PeerAuthError`, `entry_signature`, `verify_entry`, the header constants (the `peer-auth` part).

## Acceptance conditions

- AC1: a signed request verifies and any tampered method, path, body, timestamp or nonce fails with bad_signature [evidence: test:tests/test_peer_auth.py::test_sign_verify_and_tamper]
- AC2: missing or malformed headers fail with missing or malformed [evidence: test:tests/test_peer_auth.py::test_missing_and_malformed_headers]
- AC3: a timestamp outside the skew window fails with stale and one inside passes [evidence: test:tests/test_peer_auth.py::test_skew_window]
- AC4: a replayed nonce fails with replayed and a forged request does not consume its nonce [evidence: test:tests/test_peer_auth.py::test_replay_and_forgery]
- AC5: the nonce cache expires old nonces and raises NonceCacheFull rather than evicting live ones [evidence: test:tests/test_peer_auth.py::test_nonce_cache_bounds]
- AC6: entry signatures verify and a changed field or missing sig fails [evidence: test:tests/test_peer_auth.py::test_entry_signature]
- AC7: the token never appears in headers, exception texts or reprs [evidence: test:tests/test_peer_auth.py::test_token_never_exposed]

## Files

- `src/claude_code_wrapper/peer_auth.py`
- `tests/test_peer_auth.py`

## Interfaces

See the Requirement's Interfaces paragraph; keep every name and signature stated there.

## Expected outcome

Requests and directory entries can be signed and verified with the shared token, with replay and skew protection, and the token never leaves the process.

## Steps

- Write the tests named in the acceptance conditions in `tests/test_peer_auth.py` first; run `uv run pytest tests/test_peer_auth.py -q` and see them fail for the missing behaviour (not an import typo).
- Implement the smallest change that makes them pass; run `uv run pytest tests/test_peer_auth.py -q` again and expect all passed.
- Run the task verify: `uv run pytest tests/test_peer_auth.py -q && mise run lint`; expect all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

Review is full: this is the authentication of the whole feature. Pure stdlib (hmac, hashlib, secrets, json).
