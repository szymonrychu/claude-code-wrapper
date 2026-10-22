"""Peer member table and addressing (pure data structures)."""

import socket

import pytest

from claude_code_wrapper import peers
from claude_code_wrapper.peers import (
    MemberTable,
    PeerSession,
    TableFull,
    format_address,
    parse_address,
    resolve_advertise_url,
    resolve_instance_name,
)

SID = "ses_0123456789ab"
SID2 = "ses_ba9876543210"


def table(**kw) -> MemberTable:
    kw.setdefault("max_missed", 3)
    return MemberTable("me", "http://me:1", **kw)


def sess(sid=SID, name="n", cwd="/w") -> PeerSession:
    return PeerSession(sid, name, cwd)


def test_address_roundtrip():
    assert format_address("a.b-c_1", SID) == f"a.b-c_1/{SID}"
    assert parse_address(f"a.b-c_1/{SID}") == ("a.b-c_1", SID)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "x",
        f"/{SID}",
        f".{SID}",
        f"./{SID}",
        f"../{SID}",
        f".x/{SID}",
        f"a/{SID}x",
        "a/ses_xyz",
        f"a b/{SID}",
        f"{'a' * 65}/{SID}",
        f"a/b/{SID}",
        "a/SES_0123456789ab",
    ],
)
def test_bad_address(bad):
    with pytest.raises(ValueError):
        parse_address(bad)


def test_instance_name_resolution(monkeypatch):
    assert resolve_instance_name("given") == "given"
    monkeypatch.setattr(socket, "gethostname", lambda: "..My Host!.local")
    assert resolve_instance_name("") == "MyHost.local"
    monkeypatch.setattr(socket, "gethostname", lambda: "x" * 100)
    assert resolve_instance_name("") == "x" * 64
    monkeypatch.setattr(socket, "gethostname", lambda: "!!!")
    assert resolve_instance_name("") == "ccw"


def test_advertise_url(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "box")
    assert resolve_advertise_url("", 8080) == "http://box:8080"
    assert resolve_advertise_url("http://h:{port}", 9) == "http://h:9"


def test_contacts_are_not_reachable():
    t = table()
    t.add_contacts(["http://a:1", "http://me:1"])
    assert t.targets() == ["http://a:1"]
    assert t.reachable() == []
    assert t.lookup(f"a/{SID}") is None


def test_report_makes_reachable_and_replaces_sessions():
    t = table(clock=lambda: 5.0)
    assert t.record_report("a", "http://a:1", [sess()], []) == "joined"
    m = t.reachable()[0]
    assert (m.instance, m.url, m.last_seen, m.missed) == ("a", "http://a:1", 5.0, 0)
    assert m.sessions == (sess(),)
    assert t.record_report("a", "http://a:1", [sess(SID2)], []) is None
    assert t.reachable()[0].sessions == (sess(SID2),)
    assert t.targets() == ["http://a:1"]


def test_gossip_only_adds_contacts():
    t = table()
    t.record_report(
        "a",
        "http://a:1",
        [],
        [{"instance": "b", "url": "http://b:1"}, {"instance": "me", "url": "http://me:1"}],
    )
    assert [m.instance for m in t.reachable()] == ["a"]
    assert sorted(t.targets()) == ["http://a:1", "http://b:1"]


def test_missed_heartbeats_then_rejoin():
    t = table()
    t.record_report("a", "http://a:1", [sess()], [])
    assert t.record_failure("http://a:1") is None
    assert t.record_failure("http://a:1") is None
    assert t.record_failure("http://a:1") == "missed_heartbeats"
    assert t.reachable() == []
    assert "http://a:1" in t.targets()
    assert t.record_failure("http://a:1") is None
    assert t.record_report("a", "http://a:1", [sess()], []) == "joined"
    assert t.reachable()[0].missed == 0


def test_success_resets_miss_count():
    t = table()
    t.record_report("a", "http://a:1", [], [])
    t.record_failure("http://a:1")
    t.record_failure("http://a:1")
    t.record_report("a", "http://a:1", [], [])
    assert t.record_failure("http://a:1") is None
    assert len(t.reachable()) == 1


def test_leave_is_immediate_and_keeps_contact():
    t = table()
    t.record_report("a", "http://a:1", [sess()], [])
    assert t.remove("a") is True
    assert t.reachable() == []
    assert t.lookup(f"a/{SID}") is None
    assert "http://a:1" in t.targets()
    assert t.remove("a") is False
    assert t.record_report("a", "http://a:1", [sess()], []) == "joined"
    assert t.remove("http://a:1") is True
    assert t.remove("zzz") is False


def test_gossiped_contact_forgotten_seed_kept():
    t = table()
    t.add_contacts(["http://seed:1"], seed=True)
    t.add_contacts(["http://gossip:1"])
    for _ in range(29):
        t.record_failure("http://seed:1")
        t.record_failure("http://gossip:1")
    assert "http://gossip:1" in t.targets()
    for _ in range(40):
        t.record_failure("http://seed:1")
        t.record_failure("http://gossip:1")
    assert t.targets() == ["http://seed:1"]


def test_known_name_new_url():
    t = table()
    t.record_report("a", "http://a:1", [sess()], [])
    t.record_report("a", "http://a2:1", [sess()], [])
    assert [m.url for m in t.reachable()] == ["http://a2:1"]
    assert t.targets() == ["http://a2:1"]


def test_own_name_and_url_ignored():
    t = table()
    assert t.record_report("me", "http://x:1", [sess()], []) is None
    assert t.record_report("x", "http://me:1", [sess()], []) is None
    assert t.reachable() == []


def test_capacity():
    t = table(capacity=2)
    t.record_report("a", "http://a:1", [], [])
    t.record_report("b", "http://b:1", [], [])
    with pytest.raises(TableFull):
        t.record_report("c", "http://c:1", [], [])
    t.record_report("a", "http://a:1", [], [])


def test_sessions_exclude_and_filter():
    t = table()
    t.record_report("b", "http://b:1", [sess(SID2, "two", "/2")], [])
    t.record_report("a", "http://a:1", [sess(SID, "one", "/1")], [])
    allv = t.sessions()
    assert [s["address"] for s in allv] == [f"a/{SID}", f"b/{SID2}"]
    assert allv[0] == {
        "address": f"a/{SID}",
        "instance": "a",
        "session_id": SID,
        "name": "one",
        "cwd": "/1",
    }
    assert [s["instance"] for s in t.sessions(exclude=f"a/{SID}")] == ["b"]
    assert [s["instance"] for s in t.sessions(instance="b")] == ["b"]


def test_lookup():
    t = table()
    t.record_report("a", "http://a:1", [sess()], [])
    assert t.lookup(f"a/{SID}") == "http://a:1"
    assert t.lookup(f"a/{SID2}") is None
    assert t.lookup(f"q/{SID}") is None
    assert t.lookup("garbage") is None
    t.remove("a")
    assert t.lookup(f"a/{SID}") is None


def test_view_lists_only_reachable():
    t = table()
    t.record_report("a", "http://a:1", [], [])
    t.record_report("b", "http://b:1", [], [])
    t.remove("b")
    assert t.view([sess()]) == {
        "instance": "me",
        "url": "http://me:1",
        "sessions": [{"session_id": SID, "name": "n", "cwd": "/w"}],
        "members": [{"instance": "a", "url": "http://a:1"}],
    }


def test_module_exports():
    assert peers.Member and peers.ADDRESS_RE


def test_addresses_and_resolution(monkeypatch):
    test_address_roundtrip()
    for bad in ("", f"../{SID}", f".x/{SID}", f"a/{SID}x"):
        with pytest.raises(ValueError):
            parse_address(bad)
    test_instance_name_resolution(monkeypatch)
    test_advertise_url(monkeypatch)


def test_reports_and_gossip():
    test_contacts_are_not_reachable()
    test_report_makes_reachable_and_replaces_sessions()
    test_gossip_only_adds_contacts()


def test_missed_heartbeats_and_readd():
    test_missed_heartbeats_then_rejoin()
    test_success_resets_miss_count()


def test_leave_removes():
    test_leave_is_immediate_and_keeps_contact()
    test_gossiped_contact_forgotten_seed_kept()


def test_identity_rules_and_capacity():
    test_known_name_new_url()
    test_own_name_and_url_ignored()
    test_capacity()


def test_sessions_lookup_view():
    test_sessions_exclude_and_filter()
    test_lookup()
    test_view_lists_only_reachable()
