"""HMAC-SHA256 signing of peer requests (design D13).

The peer token is a signing key only: it is never sent, returned, logged or put in
an exception message. Pure stdlib, no I/O.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable, Mapping

HEADER_TIMESTAMP = "X-CCW-Peer-Timestamp"
HEADER_NONCE = "X-CCW-Peer-Nonce"
HEADER_SIGNATURE = "X-CCW-Peer-Signature"

MAX_TIMESTAMP_DIGITS = 16
_HEX = frozenset("0123456789abcdef")


class PeerAuthError(Exception):
    """A peer request failed authentication. `reason` is one of missing, malformed,
    bad_signature, stale, replayed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class NonceCacheFull(Exception):
    """The nonce cache is at capacity with no expired entry to evict."""


class NonceCache:
    """Remembers nonces for 2 x window_seconds; never evicts a live nonce."""

    def __init__(
        self,
        window_seconds: float,
        *,
        capacity: int = 65536,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._retention = 2.0 * window_seconds
        self._capacity = capacity
        self._clock = clock
        self._seen: dict[str, float] = {}  # nonce -> expiry; insertion order == expiry order

    def _evict_expired(self, now: float) -> None:
        while self._seen:
            nonce = next(iter(self._seen))
            if self._seen[nonce] > now:
                break
            del self._seen[nonce]

    def add(self, nonce: str) -> bool:
        """Record `nonce`; False when already seen. Raises NonceCacheFull at capacity."""
        now = self._clock()
        self._evict_expired(now)
        if nonce in self._seen:
            return False
        if len(self._seen) >= self._capacity:
            raise NonceCacheFull("peer nonce cache is full")
        self._seen[nonce] = now + self._retention
        return True

    def __repr__(self) -> str:
        return f"NonceCache(size={len(self._seen)}, capacity={self._capacity})"


def canonical_request(
    method: str, path: str, timestamp_ms: int | str, nonce: str, body: bytes
) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{method.upper()}\n{path}\n{timestamp_ms}\n{nonce}\n{body_hash}".encode()


def _signature(
    token: str, method: str, path: str, timestamp_ms: int | str, nonce: str, body: bytes
) -> str:
    msg = canonical_request(method, path, timestamp_ms, nonce, body)
    return hmac.new(token.encode(), msg, hashlib.sha256).hexdigest()


def sign_request(
    token: str,
    method: str,
    path: str,
    body: bytes,
    *,
    now_ms: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Return the three signing headers for a request."""
    ts = int(time.time() * 1000) if now_ms is None else now_ms
    nonce = secrets.token_hex(16) if nonce is None else nonce
    return {
        HEADER_TIMESTAMP: str(ts),
        HEADER_NONCE: nonce,
        HEADER_SIGNATURE: _signature(token, method, path, ts, nonce, body),
    }


def _header(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name)
    if value is None:
        lowered = name.lower()
        for key, val in headers.items():
            if key.lower() == lowered:
                return val
    return value


def _is_hex(value: str, length: int) -> bool:
    return len(value) == length and all(c in _HEX for c in value)


def verify_request(
    token: str,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    cache: NonceCache,
    *,
    skew_seconds: float,
    now_ms: int | None = None,
) -> None:
    """Authenticate a peer request or raise PeerAuthError. NonceCacheFull propagates."""
    ts_raw = _header(headers, HEADER_TIMESTAMP)
    nonce = _header(headers, HEADER_NONCE)
    sig = _header(headers, HEADER_SIGNATURE)
    if ts_raw is None or nonce is None or sig is None:
        raise PeerAuthError("missing")
    if (
        not (ts_raw.isascii() and ts_raw.isdigit() and len(ts_raw) <= MAX_TIMESTAMP_DIGITS)
        or not _is_hex(nonce, 32)
        or not _is_hex(sig, 64)
    ):
        raise PeerAuthError("malformed")
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if abs(now - int(ts_raw)) > skew_seconds * 1000:
        raise PeerAuthError("stale")
    expected = _signature(token, method, path, ts_raw, nonce, body)
    if not hmac.compare_digest(expected, sig):
        raise PeerAuthError("bad_signature")
    if not cache.add(nonce):
        raise PeerAuthError("replayed")
