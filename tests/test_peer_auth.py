"""Tests for HMAC peer request signing (design D13)."""

from __future__ import annotations

import hashlib

import pytest

from claude_code_wrapper.peer_auth import (
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    NonceCache,
    NonceCacheFull,
    PeerAuthError,
    canonical_request,
    sign_request,
    verify_request,
)

TOKEN = "s3cret-peer-token-value"
NOW = 1_700_000_000_000
BODY = b'{"message":"hi"}'


def _signed(**kw):
    args = dict(method="POST", path="/v1/peers/messages", body=BODY, now_ms=NOW)
    args.update(kw)
    return sign_request(
        TOKEN,
        args["method"],
        args["path"],
        args["body"],
        now_ms=args["now_ms"],
        nonce=args.get("nonce"),
    )


def _verify(
    headers,
    *,
    method="POST",
    path="/v1/peers/messages",
    body=BODY,
    cache=None,
    skew=60,
    now_ms=NOW,
    token=TOKEN,
):
    cache = cache if cache is not None else NonceCache(skew)
    verify_request(token, method, path, headers, body, cache, skew_seconds=skew, now_ms=now_ms)


def _reason(fn):
    with pytest.raises(PeerAuthError) as ei:
        fn()
    return ei.value.reason


def test_header_names():
    assert HEADER_TIMESTAMP == "X-CCW-Peer-Timestamp"
    assert HEADER_NONCE == "X-CCW-Peer-Nonce"
    assert HEADER_SIGNATURE == "X-CCW-Peer-Signature"


def test_canonical_request_format():
    got = canonical_request("post", "/p", 5, "ab", b"xy")
    assert got == b"POST\n/p\n5\nab\n" + hashlib.sha256(b"xy").hexdigest().encode()


def test_sign_headers_shape():
    h = sign_request(TOKEN, "POST", "/p", b"")
    assert set(h) == {HEADER_TIMESTAMP, HEADER_NONCE, HEADER_SIGNATURE}
    assert len(h[HEADER_NONCE]) == 32
    assert h[HEADER_NONCE] == h[HEADER_NONCE].lower()
    int(h[HEADER_NONCE], 16)
    assert h[HEADER_TIMESTAMP].isdigit()
    assert len(h[HEADER_SIGNATURE]) == 64
    assert sign_request(TOKEN, "POST", "/p", b"")[HEADER_NONCE] != h[HEADER_NONCE]


def test_round_trip():
    _verify(_signed())


def test_round_trip_case_insensitive_headers():
    h = {k.lower(): v for k, v in _signed().items()}
    _verify(h)


@pytest.mark.parametrize(
    "kw",
    [{"method": "GET"}, {"path": "/v1/peers/notices"}, {"body": b"tampered"}],
)
def test_tampered_request_bad_signature(kw):
    assert _reason(lambda: _verify(_signed(), **kw)) == "bad_signature"


def test_tampered_timestamp_and_nonce_bad_signature():
    h = _signed()
    h2 = dict(h, **{HEADER_TIMESTAMP: str(NOW + 1)})
    assert _reason(lambda: _verify(h2)) == "bad_signature"
    h3 = dict(h, **{HEADER_NONCE: "0" * 32})
    assert _reason(lambda: _verify(h3)) == "bad_signature"


def test_wrong_token_bad_signature():
    assert _reason(lambda: _verify(_signed(), token="other")) == "bad_signature"


@pytest.mark.parametrize("name", [HEADER_TIMESTAMP, HEADER_NONCE, HEADER_SIGNATURE])
def test_missing_headers(name):
    h = _signed()
    del h[name]
    assert _reason(lambda: _verify(h)) == "missing"


def test_malformed_headers():
    h = _signed()
    assert _reason(lambda: _verify(dict(h, **{HEADER_TIMESTAMP: "abc"}))) == "malformed"
    assert _reason(lambda: _verify(dict(h, **{HEADER_NONCE: "zz"}))) == "malformed"
    assert _reason(lambda: _verify(dict(h, **{HEADER_SIGNATURE: "nothex"}))) == "malformed"


def test_skew_window_edges():
    inside = _signed(now_ms=NOW - 60_000)
    _verify(inside)
    outside = _signed(now_ms=NOW - 60_001)
    assert _reason(lambda: _verify(outside)) == "stale"
    future = _signed(now_ms=NOW + 60_001)
    assert _reason(lambda: _verify(future)) == "stale"


def test_replayed_nonce():
    cache = NonceCache(60)
    h = _signed()
    _verify(h, cache=cache)
    assert _reason(lambda: _verify(h, cache=cache)) == "replayed"


def test_forged_signature_does_not_consume_nonce():
    cache = NonceCache(60)
    h = _signed()
    forged = dict(h, **{HEADER_SIGNATURE: "0" * 64})
    assert _reason(lambda: _verify(forged, cache=cache)) == "bad_signature"
    _verify(h, cache=cache)


def test_nonce_cache_expiry():
    t = [0.0]
    cache = NonceCache(10, clock=lambda: t[0])
    assert cache.add("a") is True
    assert cache.add("a") is False
    t[0] = 19.9
    assert cache.add("a") is False
    t[0] = 20.1
    assert cache.add("a") is True


def test_nonce_cache_capacity():
    t = [0.0]
    cache = NonceCache(10, capacity=2, clock=lambda: t[0])
    cache.add("a")
    cache.add("b")
    with pytest.raises(NonceCacheFull):
        cache.add("c")
    assert cache.add("a") is False  # live nonce never evicted
    t[0] = 25.0
    assert cache.add("c") is True  # expired ones evicted first


def test_nonce_cache_full_propagates_from_verify():
    cache = NonceCache(60, capacity=1)
    _verify(_signed(), cache=cache)
    with pytest.raises(NonceCacheFull):
        _verify(_signed(), cache=cache)


def test_token_never_leaks():
    h = _signed()
    assert all(TOKEN not in v for v in h.values())
    errs = []
    for kw in ({"method": "GET"}, {"body": b"x"}):
        with pytest.raises(PeerAuthError) as ei:
            _verify(h, **kw)
        errs.append(ei.value)
    with pytest.raises(PeerAuthError) as ei:
        _verify({}, token=TOKEN)
    errs.append(ei.value)
    for e in errs:
        assert TOKEN not in str(e) and TOKEN not in repr(e)
    assert TOKEN not in repr(NonceCache(60))
