"""Fake claude `{{name}}` substitution and the `ccw_service` fixture."""
# ruff: noqa: F811

from __future__ import annotations

import time

import httpx
from test_fake_claude import (  # type: ignore[import-not-found]
    env,  # noqa: F401
    spawn,  # noqa: F401
    transcript_lines,
    work,  # noqa: F401
)
from test_fake_claude_mcp import (  # type: ignore[import-not-found]
    configure,
    mcp_log,
    stub,  # noqa: F401
)


def run_named(spawn, scenario_file, match, steps, prompt):
    sc = scenario_file(
        {"boot": {"bypass_dialog": False}, "turns": [{"match": match, "steps": steps}]}
    )
    p = spawn(FAKE_CLAUDE_SCENARIO=str(sc))
    p.wait_for("? for shortcuts")
    p.submit(prompt)
    return p


def test_match_groups_substituted(spawn, scenario_file, env, work, stub) -> None:
    configure(env, url=stub.url)
    steps = [
        {"text": "hello {{who}}"},
        {
            "mcp": {
                "server": "ccw",
                "tool": "send_message",
                "arguments": {"to": "{{who}}", "nested": {"l": ["x-{{what}}", 3]}},
            }
        },
    ]
    p = run_named(
        spawn, scenario_file, r"say (?P<what>\w+) to (?P<who>\w+)", steps, "say hi to bob"
    )
    end = time.monotonic() + 15
    while not mcp_log(env) and time.monotonic() < end:
        time.sleep(0.05)
    call = [r for r in mcp_log(env)[0]["requests"] if r.get("method") == "tools/call"][0]
    assert call["params"]["arguments"] == {"to": "bob", "nested": {"l": ["x-hi", 3]}}
    p.wait_for("hello bob")
    texts = [
        c["text"]
        for x in transcript_lines(env["CLAUDE_CONFIG_DIR"], work)
        if x["type"] == "assistant"
        for c in x["message"]["content"]
        if c.get("type") == "text"
    ]
    assert "hello bob" in texts


def test_missing_group_left_literal(spawn, scenario_file, env, work) -> None:
    p = run_named(spawn, scenario_file, r"(?P<a>\w+)", [{"text": "{{a}} {{zzz}}"}], "go")
    p.wait_for("go {{zzz}}")
    q = run_named(spawn, scenario_file, r"go", [{"text": "plain {{a}}"}], "go")
    q.wait_for("plain {{a}}")


async def test_two_services_isolated(world, ccw_service) -> None:
    a = await ccw_service(world.service_cfg("a"))
    b = await ccw_service(world.service_cfg("b"))
    assert a.public_port != b.public_port
    assert a.url == f"http://127.0.0.1:{a.public_port}"
    async with httpx.AsyncClient() as c:
        assert (await c.get(f"{a.url}/healthz")).status_code == 200
        assert (await c.get(f"{b.url}/healthz")).status_code == 200
    ha = {"Authorization": "Bearer tok-a-0123456789"}
    hb = {"Authorization": "Bearer tok-b-0123456789"}
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{a.url}/v1/sessions", json={}, headers=ha)
        assert r.status_code < 300, r.text
        sid = r.json()["session_id"]
        assert (await c.get(f"{b.url}/v1/sessions/{sid}", headers=hb)).status_code == 404
    assert (world.tmp / "a" / "sessions" / sid).is_dir()
    assert not (world.tmp / "b" / "sessions" / sid).exists()
    await a.stop()
    async with httpx.AsyncClient() as c:
        assert (await c.get(f"{b.url}/healthz")).status_code == 200
