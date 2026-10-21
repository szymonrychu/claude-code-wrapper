"""K6 turn metadata: accepted on submit, given to onTurn, kept on the record."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import World, wait_turn
from fastapi.testclient import TestClient
from test_api_turns import StubManager, client, post, stub  # noqa: F401

from claude_code_wrapper.manager import CreateSessionRequest
from claude_code_wrapper.turns import TurnRecord, TurnStore


def test_old_turns_jsonl_loads_without_metadata(tmp_path: Path):
    path = tmp_path / "turns.jsonl"
    old = {"turn_id": "t1", "session_id": "s", "started_at": "2026-01-01T00:00:00Z"}
    path.write_text(json.dumps(old) + "\n")
    store = TurnStore(path)
    assert store.get("t1").metadata == {}
    store.put(TurnRecord(turn_id="t2", session_id="s", started_at="x", metadata={"k": [1]}))
    assert TurnStore(path).get("t2").metadata == {"k": [1]}


def test_api_passes_metadata(client: TestClient, stub: StubManager):  # noqa: F811
    r = post(client, {"text": "hi", "metadata": {"handoff": True}})
    assert r.status_code == 202
    assert stub.router_obj.calls[-1][-1] == {"handoff": True}
    post(client, {"text": "hi"})
    assert stub.router_obj.calls[-1][-1] == {}


def test_api_metadata_not_object_is_422(client: TestClient):  # noqa: F811
    assert post(client, {"text": "hi", "metadata": [1]}).status_code == 422
    assert post(client, {"text": "hi", "metadata": "x"}).status_code == 422


def test_api_metadata_over_16kib_is_413(client: TestClient):  # noqa: F811
    r = post(client, {"text": "hi", "metadata": {"k": "x" * 17000}})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "too_large"


def test_api_still_forbids_extra_fields(client: TestClient):  # noqa: F811
    assert post(client, {"text": "hi", "bogus": 1}).status_code == 422


async def test_metadata_reaches_onturn_record_event_and_callback(world: World):
    scenario: dict[str, Any] = {"turns": [{"match": "go", "steps": [{"text": "ok"}]}]}
    hooks = {
        "onTurn": [
            {"type": "command", "run": 'cat > "$CCW_STATE_DIR/in.$$"', "blocking": True},
        ]
    }
    m = world.manager(world.cfg(scenario, hooks=hooks))
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    router = m.router(sid)
    record = await router.submit_turn(
        "go", metadata={"handoff": "x"}, callback_url=f"{world.url}/cb"
    )
    assert record.metadata == {"handoff": "x"}
    await wait_turn(m, sid)
    await router.drain(5)
    await world.until(lambda: world.callbacks)
    stored = m.open_turns(sid).get(record.turn_id)
    assert stored.metadata == {"handoff": "x"}
    assert world.callbacks[0]["data"]["record"]["metadata"] == {"handoff": "x"}
    done = [e["data"] for e in world.events(sid) if e["type"] == "turn_completed"]
    assert done[0]["record"]["metadata"] == {"handoff": "x"}
    payloads = [
        json.loads(p.read_text())
        for p in Path(world.session_dir(sid)).rglob("in.*")
        if p.read_text().strip()
    ]
    turn_payloads = [
        p for p in payloads if p["event"] == "onTurn" and p["data"]["origin"] == "client"
    ]
    assert turn_payloads[0]["data"]["metadata"] == {"handoff": "x"}


async def test_default_metadata_empty_in_onturn_and_record(world: World):
    scenario: dict[str, Any] = {"turns": [{"match": "go", "steps": [{"text": "ok"}]}]}
    m = world.manager(world.cfg(scenario))
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    record = await m.router(sid).submit_turn("go")
    assert record.metadata == {}
    await wait_turn(m, sid)
    assert m.open_turns(sid).get(record.turn_id).metadata == {}
