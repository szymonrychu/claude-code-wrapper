"""Epic security review hardening: bounded contacts, capped reads, timestamps, rates, text."""

from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError
from test_peer_inbox import PeerHarness, msg

from claude_code_wrapper import peer_auth
from claude_code_wrapper.peer_client import (
    MAX_RESPONSE_BYTES,
    MemberView,
    PeerClient,
    PeerError,
)
from claude_code_wrapper.peers import MemberTable, PeerSession

TOKEN = "s3cret-peer-token-value"
SID = "ses_0123456789ab"


def table(capacity=3, max_missed=2):
    return MemberTable("me", "http://me:1", max_missed=max_missed, capacity=capacity)


# --- contacts --------------------------------------------------------------


def test_gossip_contacts_bounded():
    t = table(capacity=3)
    t.add_contacts(["http://g1:1", "http://g2:1", "http://g3:1", "http://g4:1"])
    assert t.targets() == ["http://g1:1", "http://g2:1", "http://g3:1"]
    t.add_contacts(["http://seed:1"], seed=True)
    assert "http://seed:1" in t.targets()
    assert len(t.targets()) == 3
    assert "http://g1:1" not in t.targets()  # unconfirmed gossip evicted first


def test_answering_peer_evicts_unconfirmed_gossip():
    t = table(capacity=2)
    t.add_contacts(["http://g1:1", "http://g2:1"])
    t.record_report("beta", "http://beta:1", [PeerSession(SID, "n", "/")])
    assert "http://beta:1" in t.targets()
    assert len(t.targets()) == 2


def test_gossip_only_contact_forgotten_after_max_missed():
    t = table(capacity=10, max_missed=2)
    t.add_contacts(["http://g1:1"])
    t.record_failure("http://g1:1")
    assert "http://g1:1" in t.targets()
    t.record_failure("http://g1:1")
    assert "http://g1:1" not in t.targets()


def test_seed_never_forgotten():
    t = table(max_missed=1)
    t.add_contacts(["http://s:1"], seed=True)
    for _ in range(50):
        t.record_failure("http://s:1")
    assert "http://s:1" in t.targets()


# --- client ----------------------------------------------------------------


def _view():
    return MemberView.model_validate({"instance": "alpha", "url": "http://alpha:8080"})


async def test_oversize_answer_unreachable():
    big = b'{"x": "' + b"a" * (MAX_RESPONSE_BYTES + 10) + b'"}'
    client = PeerClient(
        TOKEN, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=big))
    )
    with pytest.raises(PeerError) as ei:
        await client.heartbeat("http://b:1", _view())
    assert ei.value.code == "unreachable"
    await client.aclose()


async def test_normal_answer_still_parsed():
    payload = {"instance": "beta", "url": "http://beta:1"}
    client = PeerClient(
        TOKEN, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    )
    assert (await client.heartbeat("http://b:1", _view())).instance == "beta"
    await client.aclose()


def test_remote_text_bounded_and_clean():
    base = {"instance": "a", "url": "http://a:1"}
    with pytest.raises(ValidationError):
        MemberView.model_validate(
            {**base, "sessions": [{"session_id": SID, "name": "n" * 201, "cwd": "/"}]}
        )
    with pytest.raises(ValidationError):
        MemberView.model_validate(
            {**base, "sessions": [{"session_id": SID, "name": "n", "cwd": "/" * 1025}]}
        )
    v = MemberView.model_validate(
        {**base, "sessions": [{"session_id": SID, "name": "a\x1b[31mb\nc", "cwd": "/x\x00y"}]}
    )
    assert v.sessions[0].name == "a[31mbc"
    assert v.sessions[0].cwd == "/xy"


# --- auth ------------------------------------------------------------------


def test_huge_timestamp_is_malformed():
    headers = peer_auth.sign_request(TOKEN, "POST", "/p", b"")
    headers[peer_auth.HEADER_TIMESTAMP] = "9" * 5000
    cache = peer_auth.NonceCache(60)
    with pytest.raises(peer_auth.PeerAuthError) as ei:
        peer_auth.verify_request(TOKEN, "POST", "/p", headers, b"", cache, skew_seconds=60)
    assert ei.value.reason == "malformed"


# --- inbox -----------------------------------------------------------------


async def test_total_rate_limit_and_bounded_rate_map(tmp_path):
    h = PeerHarness(tmp_path, max_items=500, max_per_minute=100, total=3)
    for i in range(3):
        h.inbox.put(msg(i, sender=f"s{i}/ses_0123456789ab"))
    with pytest.raises(PeerError) as ei:
        h.inbox.put(msg(9, sender="s9/ses_0123456789ab"))
    assert ei.value.code == "rate_limited"
    await h.router.close()
    from claude_code_wrapper import peer_inbox

    h = PeerHarness(_sub(tmp_path), max_items=5000, max_per_minute=100, total=10**6)
    for i in range(peer_inbox.MAX_RATE_SENDERS + 20):
        h.inbox.put(msg(i, sender=f"s{i}/ses_0123456789ab"))
    assert len(h.inbox._rate) <= peer_inbox.MAX_RATE_SENDERS
    await h.router.close()


def _sub(path):
    sub = path / "b"
    sub.mkdir()
    return sub
