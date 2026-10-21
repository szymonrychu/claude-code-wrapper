"""K9: configurable probe token and the captured answer (design D13)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from test_api_turns import SID, client, stub  # noqa: F401,F811
from test_router import Harness as RouterHarness
from test_router import assistant, user, wait_until
from test_session_turns import Harness, harness  # noqa: F401  (pytest fixture)

from claude_code_wrapper.config import Config, SessionsConfig
from claude_code_wrapper.pty_proc import paste_bytes
from claude_code_wrapper.turns import Probe, now_iso


def test_config_default_and_non_empty():
    assert Config().sessions.probe_alive_token == "CCW-ALIVE"
    with pytest.raises(ValidationError):
        SessionsConfig(probe_alive_token="")


def test_probe_answer_defaults_none():
    assert Probe(probe_id="p", text="t", created_at=now_iso()).answer is None


async def test_default_probe_text_names_configured_token(harness: Harness):  # noqa: F811
    session = await harness.start({}, sessions={"probe_alive_token": "TOK-9"})
    probe = await session.probe()
    assert probe.text == "Reply with TOK-9 and one sentence on what you are doing, then continue."
    assert harness.writes[0] == paste_bytes(f"{probe.text}\n\n<!-- ccw probe: {probe.probe_id} -->")
    session.probe_delivered(probe.probe_id)
    assert session.probe_answered("TOK-9 fine") == [probe]
    assert probe.answer == "TOK-9 fine" and probe.answered_at


async def test_answer_capped_at_2000(harness: Harness):  # noqa: F811
    session = await harness.start({})
    probe = await session.probe()
    session.probe_delivered(probe.probe_id)
    session.probe_answered("CCW-ALIVE" + "x" * 5000)
    assert probe.answer is not None and len(probe.answer) == 2000


async def test_router_answers_with_configured_token(tmp_path):
    h = RouterHarness(tmp_path, sessions={"probe_alive_token": "TOK-9"})
    from claude_code_wrapper.turns import Probe as P

    h.session.probes["probe-1"] = P(probe_id="probe-1", text="ping", created_at=now_iso())
    h.router.ensure_tailing()
    h.transcript.write_text(
        user("ping\n\n<!-- ccw probe: probe-1 -->") + assistant("CCW-ALIVE only default")
    )
    await wait_until(lambda: h.events("assistant_text"))
    assert not h.events("probe")  # the default token no longer answers
    h.transcript.write_text(h.transcript.read_text() + assistant("all good TOK-9 here", mid="m2"))
    await wait_until(lambda: h.events("probe"))
    (ev,) = h.events("probe")
    assert ev.data["answer"] == "all good TOK-9 here" and ev.data["state"] == "answered"
    await h.router.close()


def test_get_probe_includes_answer(client, stub):  # noqa: F811
    client.post(f"/v1/sessions/{SID}/probe")
    got = client.get(f"/v1/sessions/{SID}/probe/probe-1").json()
    assert got["answer"] is None
    probe = stub.router_obj.session.probes["probe-1"]
    probe.answer = "CCW-ALIVE yes"
    got = client.get(f"/v1/sessions/{SID}/probe/probe-1").json()
    assert got["answer"] == "CCW-ALIVE yes"
