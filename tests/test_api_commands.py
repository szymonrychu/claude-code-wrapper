"""Public commands routes over a real manager, router and fake claude."""

from __future__ import annotations

import asyncio

import httpx
from conftest import World
from test_command_runner import commands_cfg
from test_command_turn_resume import CONTINUE, GO, RESUMED

from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.manager import CreateSessionRequest

TOKEN = "tok-0123456789abcdef"
H = {"Authorization": f"Bearer {TOKEN}"}


async def boot(world: World, *, commands: dict | None = None, turns: list | None = None, **cmds):
    scenario = {"commands": commands or {}}
    if turns is not None:
        scenario["turns"] = turns
    cfg = world.cfg(
        scenario,
        server={"auth_token": TOKEN},
        sessions={"commands": commands_cfg(continue_prompt=CONTINUE, **cmds)},
    )
    m = world.manager(cfg)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    app = create_app(m, cfg)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H
    )
    return m, sid, m.router(sid), client


async def settled(client: httpx.AsyncClient, url: str) -> dict:
    for _ in range(100):
        rec = (await client.get(url)).json()
        if rec["state"] in ("completed", "failed", "cancelled"):
            return rec
        await asyncio.sleep(0.1)
    return rec


async def test_post_command_returns_202_record(world):
    m, sid, router, c = await boot(world)
    async with c:
        r = await c.post(f"/v1/sessions/{sid}/commands", json={"name": "reload-plugins"})
        assert r.status_code == 202
        body = r.json()
        assert body["origin"] == "client" and body["name"] == "reload-plugins"
        assert body["args"] == {} and body["id"].startswith("cmd_")
        # idle session: it runs at once and the record read back shows the final state
        final = await settled(c, f"/v1/sessions/{sid}/commands/{body['id']}")
        assert final["state"] == "completed"
        extra = await c.post(f"/v1/sessions/{sid}/commands", json={"name": "compact", "x": 1})
        assert extra.status_code == 422 and extra.json()["error"]["code"] == "invalid_request"
        bad = await c.post(f"/v1/sessions/{sid}/commands", json={"name": "compact", "args": "x"})
        assert bad.status_code == 422


async def test_post_command_not_allowed_403(world):
    m, sid, router, c = await boot(world, client=["compact"])
    async with c:
        r = await c.post(f"/v1/sessions/{sid}/commands", json={"name": "reload-plugins"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "command_not_allowed"


async def test_post_command_bad_args_422(world):
    m, sid, router, c = await boot(world)
    async with c:
        for body in (
            {"name": "compact", "args": {"nope": 1}},
            {"name": "reload-plugins", "args": {"force": True}},
            {"name": "nonsense"},
        ):
            r = await c.post(f"/v1/sessions/{sid}/commands", json=body)
            assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_command"


async def test_post_command_queue_full_429(world):
    scenario = {"turns": [{"match": "^go", "steps": [{"sleep": 3}, {"text": "x"}]}]}
    m, sid, router, c = await boot(world, turns=scenario["turns"])
    async with c:
        await router.submit_turn("go")
        names = ("compact", "reload-plugins", "reload-skills", "context")
        for n in names:
            assert (
                await c.post(f"/v1/sessions/{sid}/commands", json={"name": n})
            ).status_code == 202
        r = await c.post(
            f"/v1/sessions/{sid}/commands", json={"name": "mcp", "args": {"action": "reconnect"}}
        )
        assert r.status_code == 429 and r.json()["error"]["code"] == "command_queue_full"


async def test_post_command_not_running_409(world):
    m, sid, router, c = await boot(world)
    async with c:
        url = f"/v1/sessions/{sid}/commands"
        real = router.session.state
        router.session.state = "booting"
        try:
            r = await c.post(url, json={"name": "compact"})
            assert r.status_code == 409 and r.json()["error"]["code"] == "booting"
            router.session.state = "dead"
            r = await c.post(url, json={"name": "compact"})
            assert r.status_code == 409 and r.json()["error"]["code"] == "dead"
        finally:
            router.session.state = real


async def test_unknown_session_404(world):
    m, sid, router, c = await boot(world)
    async with c:
        assert (
            await c.post("/v1/sessions/nope/commands", json={"name": "compact"})
        ).status_code == 404
        assert (await c.get("/v1/sessions/nope/commands")).status_code == 404


async def test_get_command_and_list(world):
    m, sid, router, c = await boot(world)
    async with c:
        url = f"/v1/sessions/{sid}/commands"
        a = (await c.post(url, json={"name": "reload-plugins"})).json()
        await settled(c, f"{url}/{a['id']}")
        b = (await c.post(url, json={"name": "reload-skills"})).json()
        final = await settled(c, f"{url}/{b['id']}")
        assert final["state"] == "completed" and final["id"] == b["id"]
        listed = (await c.get(url)).json()["commands"]
        assert [x["id"] for x in listed] == [a["id"], b["id"]]


async def test_unknown_command_id_404(world):
    m, sid, router, c = await boot(world)
    async with c:
        r = await c.get(f"/v1/sessions/{sid}/commands/cmd_missing")
        assert r.status_code == 404 and r.json()["error"]["code"] == "command_not_found"


async def test_commands_require_auth(world):
    m, sid, router, c = await boot(world)
    async with c:
        url = f"/v1/sessions/{sid}/commands"
        assert (
            await c.post(url, json={"name": "compact"}, headers={"Authorization": ""})
        ).status_code == 401
        assert (
            await c.get(url, headers={"Authorization": "Bearer wrong-token-0000000"})
        ).status_code == 401


async def test_client_command_during_turn_resumes_turn(world):
    m, sid, router, c = await boot(world, turns=[GO, RESUMED])
    async with c:
        turn = await router.submit_turn("go")
        r = await c.post(f"/v1/sessions/{sid}/commands", json={"name": "compact"})
        assert r.status_code == 202 and r.json()["state"] == "queued"
        assert r.json()["turn_id"] == turn.turn_id
        done = await world.wait_turn(m, sid)
        assert done.turn_id == turn.turn_id and done.final_text == "part two"
        got = (await c.get(f"/v1/sessions/{sid}/commands/{r.json()['id']}")).json()
        assert got["state"] == "completed"
