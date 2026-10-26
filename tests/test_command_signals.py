"""SignalBus and the router's compaction and command completion signals."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from test_router import Harness

from claude_code_wrapper.bootstrap.settings import render_settings
from claude_code_wrapper.command_signals import SignalBus
from claude_code_wrapper.config import Config
from claude_code_wrapper.hook_relay import CLAUDE_HOOK_EVENTS
from claude_code_wrapper.internal_api import create_internal_app
from claude_code_wrapper.metrics import Metrics
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


async def test_pre_compact_emits_compacting(h):
    for trigger in ("auto", "manual"):
        before = len(h.events("compacting"))
        r = await h.router.handle_claude_hook(
            "PreCompact", {"trigger": trigger, "custom_instructions": "keep"}
        )
        evs = h.events("compacting")
        assert len(evs) == before + 1
        assert evs[-1].data == {"trigger": trigger}
        assert r["exit_code"] == 0 and r["stdout"] == ""
    assert h.events("compacted") == []


async def test_compacting_trigger_normalised(h):
    await h.router.handle_claude_hook("PreCompact", {})
    await h.router.handle_claude_hook("PreCompact", {"trigger": "weird"})
    await h.router.handle_claude_hook("PreCompact", {"trigger": 7})
    assert [e.data for e in h.events("compacting")] == [{"trigger": "unknown"}] * 3
    before = len(h.events("compacting"))
    await h.router.handle_claude_hook("PostCompact", {"trigger": "auto", "compact_summary": "s"})
    assert len(h.events("compacting")) == before


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


def _boundary_data(trigger="auto"):
    return {"trigger": trigger, "pre_tokens": 100, "post_tokens": 10, "duration_ms": 5}


async def test_compact_relays_installed_and_accepted(tmp_path):
    doc = render_settings(tmp_path, Config(), relay_command="/opt/ccw-hook")
    for event in ("PreCompact", "PostCompact"):
        assert event in CLAUDE_HOOK_EVENTS
        (group,) = doc["hooks"][event]
        assert group["hooks"][0]["command"] == f"/opt/ccw-hook {event}"

    seen = []

    class R:
        async def handle_claude_hook(self, event, payload):
            seen.append(event)
            return {"stdout": "", "stderr": "", "exit_code": 0}

    class M:
        def hook_token_matches(self, sid, token):
            return True

        def router(self, sid):
            return R()

    app = create_internal_app(M())  # type: ignore[arg-type]
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        hdr = {"X-CCW-Session": "s1", "X-CCW-Hook-Token": "t"}
        for event in ("PreCompact", "PostCompact"):
            r = await c.post(f"/internal/hooks/{event}", json={}, headers=hdr)
            assert r.status_code == 200
    assert seen == ["PreCompact", "PostCompact"]


async def test_boundary_emits_compacted_event(h):
    metrics = Metrics()
    h.router._metrics = metrics
    ev = CompactBoundary(trigger="manual", pre_tokens=100, post_tokens=10, duration_ms=5)
    await h.router._on_transcript_events([ev])
    (e,) = h.events("compacted")
    assert e.data == _boundary_data("manual")
    assert 'ccw_compactions_total{trigger="manual"} 1.0' in metrics.render().decode()


async def test_auto_compaction_reported(h):
    metrics = Metrics()
    h.router._metrics = metrics
    await h.router.handle_claude_hook("PreCompact", {"trigger": "auto"})
    await h.router._on_transcript_events(
        [CompactBoundary(trigger="auto", pre_tokens=100, post_tokens=10, duration_ms=5)]
    )
    await h.router.handle_claude_hook("PostCompact", {"trigger": "auto"})
    (e,) = h.events("compacted")
    assert e.data == _boundary_data("auto")
    assert 'ccw_compactions_total{trigger="auto"} 1.0' in metrics.render().decode()


async def test_compact_relays_publish_signals(h):
    await test_pre_and_post_compact_relays_publish(h)


async def test_command_lines_publish_signals(h):
    await h.router._on_transcript_events(
        [CommandInvoked(name="compact", args="x"), CommandOutput(stdout="o", stderr="e")]
    )
    kinds = [
        (await h.router.signals.wait(lambda s: True, after=i, timeout=0.1)).kind for i in (0, 1)
    ]
    assert kinds == ["command_invoked", "command_output"]


async def test_signal_bus_wait_after_mark():
    bus = SignalBus()
    mark = bus.mark()
    published = bus.publish("post_compact", {"x": 1})
    sig = await bus.wait(lambda s: s.kind == "post_compact", after=mark, timeout=1)
    assert sig == published and sig.seq == mark + 1


async def test_signal_bus_timeout_and_bound():
    bus = SignalBus()
    assert await bus.wait(lambda s: True, after=0, timeout=0.01) is None
    for i in range(100):
        bus.publish("command_output", {"i": i})
    assert await bus.wait(lambda s: s.data["i"] < 36, after=0, timeout=0.01) is None
    assert (await bus.wait(lambda s: True, after=0, timeout=0.01)).data["i"] == 36


async def test_sidechain_lines_ignored(h):
    await h.router._on_transcript_events(
        [
            CommandInvoked(name="s", sidechain=True),
            CommandOutput(stdout="s", sidechain=True),
            CompactBoundary(trigger="auto", sidechain=True),
        ]
    )
    assert h.router.signals.mark() == 0
    assert h.events("compacted") == []
