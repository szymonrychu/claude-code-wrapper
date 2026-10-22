"""Peer metric recorders: each updates its family with the given labels; bad labels raise."""

from __future__ import annotations

import pytest

from claude_code_wrapper.metrics import Metrics


def _value(m: Metrics, sample: str) -> float | None:
    for line in m.render().decode().splitlines():
        if line.startswith(sample + " "):
            return float(line.rsplit(" ", 1)[1])
    return None


@pytest.mark.parametrize(
    ("recorder", "family"),
    [
        ("peer_received", "ccw_peer_messages_received_total"),
        ("peer_sent", "ccw_peer_messages_sent_total"),
        ("peer_delivered", "ccw_peer_messages_delivered_total"),
    ],
)
@pytest.mark.parametrize("kind", ["message", "notice"])
def test_message_recorders(recorder: str, family: str, kind: str) -> None:
    m = Metrics()
    getattr(m, recorder)(kind, "ok")
    getattr(m, recorder)(kind, "ok")
    assert _value(m, f'{family}{{kind="{kind}",result="ok"}}') == 2.0


@pytest.mark.parametrize("op", ["join", "heartbeat", "leave", "inbound_heartbeat"])
def test_peer_membership(op: str) -> None:
    m = Metrics()
    m.peer_membership(op, "failed")
    assert _value(m, f'ccw_peer_membership_total{{op="{op}",result="failed"}}') == 1.0


def test_peer_members_gauge() -> None:
    m = Metrics()
    m.peer_members(3)
    m.peer_members(2)
    assert _value(m, "ccw_peer_members") == 2.0


@pytest.mark.parametrize("recorder", ["peer_received", "peer_sent", "peer_delivered"])
def test_bad_kind_raises(recorder: str) -> None:
    with pytest.raises(ValueError):
        getattr(Metrics(), recorder)("typo", "ok")


def test_bad_op_raises() -> None:
    with pytest.raises(ValueError):
        Metrics().peer_membership("typo", "ok")
