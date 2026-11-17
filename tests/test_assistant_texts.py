"""assistant_text event turn_id/sidechain and the onStop assistant_texts buffer."""

from __future__ import annotations

from typing import Any

from conftest import World, wait_turn
from test_stop_gate import Gate, of_type, start, turn

from claude_code_wrapper.transcript import AssistantText

MAX_BLOCKS = 200
MAX_BYTES = 256 * 1024


def ev(text: str, mid: str, **kw: Any) -> AssistantText:
    return AssistantText(text=text, message_id=mid, **kw)


async def running(world: World) -> tuple[Any, str, Any]:
    scenario = turn([{"text": "hold"}, {"sleep": 30}])
    m, sid = await start(world, world.cfg(scenario))
    router = m.router(sid)
    record = await router.submit_turn("go")
    await world.until(lambda: of_type(world, sid, "assistant_text"))
    return m, sid, record


async def test_assistant_text_carries_turn_id_and_sidechain(world: World):
    m, sid, record = await running(world)
    router = m.router(sid)
    (first,) = of_type(world, sid, "assistant_text")
    assert first["turn_id"] == record.turn_id and first["sidechain"] is False
    await router._on_assistant_text(ev("side", "m-side", sidechain=True, agent_id="a1"))
    side = of_type(world, sid, "assistant_text")[-1]
    assert side["sidechain"] is True and side["turn_id"] == record.turn_id
    await router.interrupt()


async def test_assistant_text_turn_id_null_outside_a_turn(world: World):
    m, sid = await start(world, world.cfg(turn([{"text": "x"}])))
    router = m.router(sid)
    await router._on_assistant_text(ev("late", "m1"))
    (got,) = of_type(world, sid, "assistant_text")
    assert got["turn_id"] is None and got["sidechain"] is False
    assert router._assistant_text_data() == {
        "assistant_texts": [],
        "assistant_texts_truncated": False,
    }


async def test_on_stop_holds_main_chain_blocks_in_order_and_resets_per_turn(world: World):
    gate = Gate(world.tmp)
    scenario = {
        "turns": [
            {"match": "one", "steps": [{"text": "a"}, {"text": "b"}]},
            {"match": "two", "steps": [{"text": "c"}]},
        ]
    }
    m, sid = await start(world, world.cfg(scenario, hooks={"onStop": [gate.action()]}))
    router = m.router(sid)
    await router.submit_turn("one")
    await wait_turn(m, sid)
    await router.submit_turn("two")
    await world.until(lambda: len(Gate.calls(world, sid)) == 2)
    first, second = Gate.calls(world, sid)
    assert [b["text"] for b in first["data"]["assistant_texts"]] == ["a", "b"]
    assert all(set(b) == {"message_id", "text"} for b in first["data"]["assistant_texts"])
    assert first["data"]["assistant_texts_truncated"] is False
    assert [b["text"] for b in second["data"]["assistant_texts"]] == ["c"]


async def test_sidechain_blocks_are_not_buffered(world: World):
    m, sid, _ = await running(world)
    router = m.router(sid)
    await router._on_assistant_text(ev("side", "m-s", sidechain=True))
    await router._on_assistant_text(ev("agent", "m-a", agent_id="a1"))
    texts = [b["text"] for b in router._assistant_text_data()["assistant_texts"]]
    assert texts == ["hold"]
    await router.interrupt()


async def test_block_cap_sets_truncated_and_drops_oldest(world: World):
    m, sid, _ = await running(world)
    router = m.router(sid)
    for i in range(MAX_BLOCKS + 5):
        await router._on_assistant_text(ev(f"t{i}", f"m{i}"))
    data = router._assistant_text_data()
    assert len(data["assistant_texts"]) == MAX_BLOCKS
    assert data["assistant_texts_truncated"] is True
    assert data["assistant_texts"][-1]["text"] == f"t{MAX_BLOCKS + 4}"
    assert data["assistant_texts"][0]["text"] == "t5"
    await router.interrupt()


async def test_byte_cap_sets_truncated(world: World):
    m, sid, _ = await running(world)
    router = m.router(sid)
    chunk = "x" * (64 * 1024)
    for i in range(6):
        await router._on_assistant_text(ev(chunk, f"m{i}"))
    data = router._assistant_text_data()
    total = sum(len(b["text"].encode()) for b in data["assistant_texts"])
    assert total <= MAX_BYTES
    assert data["assistant_texts_truncated"] is True
    assert data["assistant_texts"][-1]["message_id"] == "m5"
    await router.interrupt()
