"""K3: DELETE hook_input into onDestroy, hook_outputs back, destroy_output event (design D7)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from conftest import World, wait_turn
from fastapi.testclient import TestClient

from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.config import Config
from claude_code_wrapper.events import EVENT_TYPES
from claude_code_wrapper.manager import CreateFailed, CreateSessionRequest, CreateVetoed

TOKEN = "tok-0123456789abcdef"
ECHO_INPUT = {
    "type": "command",
    "run": 'python3 -c "import json,sys; d=json.load(sys.stdin); '
    "print(json.dumps({'output': {'seen': d['data']['input']}}))\"",
    "blocking": True,
}


def types(world: World, sid: str) -> list[str]:
    return [e["type"] for e in world.events(sid)]


def test_destroy_output_is_an_event_type() -> None:
    assert "destroy_output" in EVENT_TYPES


async def test_destroy_passes_input_and_returns_outputs(world: World) -> None:
    m = world.manager(world.cfg(hooks={"onDestroy": [ECHO_INPUT]}))
    snap = await m.create(CreateSessionRequest(prompt="hi"))
    sid = snap["session_id"]
    await wait_turn(m, sid)
    out = await m.destroy(sid, hook_input={"review": "go"})
    assert out["state"] == "stopped"
    assert len(out["hook_outputs"]) == 1
    entry = out["hook_outputs"][0]
    assert entry["ok"] is True
    assert entry["index"] == 0
    assert entry["output"] == {"seen": {"review": "go"}}
    ev = types(world, sid)
    assert ev.count("session_closed") == 1
    assert ev.index("destroy_output") < ev.index("session_closed")
    assert ev[-1] == "session_closed"
    do = [e for e in world.events(sid) if e["type"] == "destroy_output"]
    assert do[0]["data"]["outputs"] == out["hook_outputs"]


async def test_destroy_without_input_is_null_and_repeat_is_empty(world: World) -> None:
    m = world.manager(world.cfg(hooks={"onDestroy": [ECHO_INPUT]}))
    sid = (await m.create(CreateSessionRequest(prompt="hi")))["session_id"]
    await wait_turn(m, sid)
    out = await m.destroy(sid)
    assert out["hook_outputs"][0]["output"] == {"seen": None}
    again = await m.destroy(sid)
    assert again["hook_outputs"] == []
    assert types(world, sid).count("session_closed") == 1


async def test_non_blocking_outputs_not_returned(world: World, tmp_path: Path) -> None:
    hook = {**ECHO_INPUT, "blocking": False}
    m = world.manager(world.cfg(hooks={"onDestroy": [hook]}))
    sid = (await m.create(CreateSessionRequest(prompt="hi")))["session_id"]
    await wait_turn(m, sid)
    out = await m.destroy(sid, hook_input={"a": 1})
    assert out["hook_outputs"] == []


async def test_no_hooks_gives_empty_outputs(world: World) -> None:
    m = world.manager(world.cfg())
    sid = (await m.create(CreateSessionRequest(prompt="hi")))["session_id"]
    await wait_turn(m, sid)
    assert (await m.destroy(sid))["hook_outputs"] == []


async def test_shutdown_runs_on_destroy_with_null_input(world: World) -> None:
    m = world.manager(world.cfg(hooks={"onDestroy": [ECHO_INPUT]}))
    sid = (await m.create(CreateSessionRequest(prompt="hi")))["session_id"]
    await wait_turn(m, sid)
    await m.shutdown()
    ev = types(world, sid)
    assert ev.count("session_closed") == 1
    do = [e for e in world.events(sid) if e["type"] == "destroy_output"]
    assert do and do[0]["data"]["outputs"][0]["output"] == {"seen": None}
    assert ev.index("destroy_output") < ev.index("session_closed")


async def test_failed_create_emits_one_session_closed(world: World, tmp_path: Path) -> None:
    dead = tmp_path / "dead-claude"
    dead.write_text("#!/bin/sh\nexit 1\n")
    dead.chmod(0o755)
    m = world.manager(world.cfg(claude={"binary": str(dead)}))
    with pytest.raises(CreateFailed):
        await m.create(CreateSessionRequest())
    [snap] = m.list(state="failed")
    assert types(world, snap["session_id"]).count("session_closed") == 1


async def test_vetoed_create_emits_one_session_closed(world: World) -> None:
    deny = {
        "type": "command",
        "run": 'echo \'{"decision": "deny", "reason": "no"}\'',
        "blocking": True,
    }
    m = world.manager(world.cfg(hooks={"onCreate": [deny]}))
    with pytest.raises(CreateVetoed):
        await m.create(CreateSessionRequest())
    [snap] = m.list()
    assert types(world, snap["session_id"]).count("session_closed") == 1


async def test_abandoned_destroy_emits_one_session_closed(world: World) -> None:
    slow = {"type": "command", "run": "cat > /dev/null; sleep 30", "blocking": True}
    m = world.manager(
        world.cfg(
            hooks={"onDestroy": [{**slow, "timeout_seconds": 30}]},
            limits={"shutdown_timeout_seconds": 2},
        )
    )
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    closing = asyncio.ensure_future(m.destroy(sid))
    await asyncio.sleep(0.5)  # inside the slow onDestroy: session_closed not yet emitted
    await m.shutdown()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert types(world, sid).count("session_closed") == 1


class Stub:
    started = True

    def __init__(self) -> None:
        from claude_code_wrapper.redact import Redactor

        self.redactor = Redactor()
        self.calls: list[tuple] = []

    async def destroy(self, sid, purge=False, hook_input=None):
        self.calls.append((sid, purge, hook_input))
        return {"session_id": sid, "state": "stopped", "hook_outputs": []}


@pytest.fixture
def api() -> tuple[TestClient, Stub]:
    cfg = Config.model_validate(
        {"server": {"auth_token": TOKEN, "callback_secret": "cb-secret-0123456789"}}
    )
    stub = Stub()
    return TestClient(create_app(stub, cfg), headers={"Authorization": f"Bearer {TOKEN}"}), stub


def test_api_delete_body_forwarded(api) -> None:
    client, stub = api
    r = client.request("DELETE", "/v1/sessions/ses_1", json={"hook_input": {"x": 1}})
    assert r.status_code == 200
    assert stub.calls[-1] == ("ses_1", False, {"x": 1})
    client.delete("/v1/sessions/ses_1?purge=true")
    assert stub.calls[-1] == ("ses_1", True, None)


def test_api_delete_rejects_extra_keys(api) -> None:
    client, _ = api
    r = client.request("DELETE", "/v1/sessions/ses_1", json={"nope": 1})
    assert r.status_code == 422


def test_api_delete_body_too_large(api) -> None:
    client, stub = api
    body = json.dumps({"hook_input": {"blob": "x" * (1024 * 1024 + 10)}})
    r = client.request(
        "DELETE", "/v1/sessions/ses_1", content=body, headers={"content-type": "application/json"}
    )
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "too_large"
    assert stub.calls == []
