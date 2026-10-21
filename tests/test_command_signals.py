"""SignalBus and the router's compaction and command completion signals."""

from __future__ import annotations

import asyncio

import pytest
from test_router import Harness

from claude_code_wrapper.command_signals import SignalBus
from claude_code_wrapper.transcript import CommandInvoked, CommandOutput, CompactBoundary


@pytest.fixture
async def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    await harness.router.close()


async def test_wait_sees_signal_published_after_mark():
    bus = SignalBus()
    mark = bus.mark()
    bus.publish("pre_compact", {"trigger": "manual"})
    sig = await bus.wait(lambda s: s.kind == "pre_compact", after=mark, timeout=1)
    assert sig is not None and sig.data == {"trigger": "manual"} and sig.seq == mark + 1


async def test_wait_ignores_signals_at_or_before_after():
    bus = SignalBus()
    bus.publish("post_compact", {})
    assert await bus.wait(lambda s: True, after=bus.mark(), timeout=0.05) is None


async def test_wait_wakes_on_later_publish_and_filters():
    bus = SignalBus()
    task = asyncio.create_task(bus.wait(lambda s: s.kind == "command_output", after=0, timeout=2))
    await asyncio.sleep(0.01)
    bus.publish("command_invoked", {})
    bus.publish("command_output", {"stdout": "x"})
    sig = await task
    assert sig is not None and sig.kind == "command_output"


async def test_history_bounded_to_64():
    bus = SignalBus()
    for i in range(100):
        bus.publish("command_output", {"i": i})
    assert await bus.wait(lambda s: s.data["i"] == 0, after=0, timeout=0.01) is None
    assert (await bus.wait(lambda s: s.data["i"] == 36, after=0, timeout=0.01)) is not None
    assert bus.mark() == 100


async def test_pre_and_post_compact_relays_publish(h):
    r1 = await h.router.handle_claude_hook(
        "PreCompact", {"trigger": "auto", "custom_instructions": "keep"}
    )
    r2 = await h.router.handle_claude_hook(
        "PostCompact", {"trigger": "auto", "compact_summary": "sum"}
    )
    assert r1["exit_code"] == 0 and r2["exit_code"] == 0 and r1["stdout"] == ""
    pre = await h.router.signals.wait(lambda s: s.kind == "pre_compact", after=0, timeout=0.1)
    post = await h.router.signals.wait(lambda s: s.kind == "post_compact", after=0, timeout=0.1)
    assert pre.data == {"trigger": "auto", "custom_instructions": "keep"}
    assert post.data == {"trigger": "auto", "compact_summary": "sum"}
    assert h.events("compacted") == []


async def test_compact_boundary_emits_once_counts_and_publishes(h):
    await h.router.handle_claude_hook("PostCompact", {"trigger": "auto"})
    ev = CompactBoundary(trigger="auto", pre_tokens=100, post_tokens=10, duration_ms=5)
    await h.router._on_transcript_events([ev])
    (e,) = h.events("compacted")
    assert e.data == {"trigger": "auto", "pre_tokens": 100, "post_tokens": 10, "duration_ms": 5}
    sig = await h.router.signals.wait(lambda s: s.kind == "compact_boundary", after=0, timeout=0.1)
    assert sig.data == e.data


async def test_command_entries_publish_main_chain_only(h):
    await h.router._on_transcript_events(
        [
            CommandInvoked(name="compact", args="x"),
            CommandOutput(stdout="out", stderr="err"),
            CommandInvoked(name="side", sidechain=True),
            CommandOutput(stdout="side", sidechain=True),
            CompactBoundary(trigger="auto", sidechain=True),
        ]
    )
    assert h.router.signals.mark() == 2
    inv = await h.router.signals.wait(lambda s: s.kind == "command_invoked", after=0, timeout=0.1)
    out = await h.router.signals.wait(lambda s: s.kind == "command_output", after=0, timeout=0.1)
    assert inv.data == {"name": "compact", "args": "x"}
    assert out.data == {"stdout": "out", "stderr": "err"}
    assert h.events("compacted") == []
