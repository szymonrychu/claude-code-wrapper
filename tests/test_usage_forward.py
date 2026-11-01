"""UsageForwarder: statusLine rate_limits forwarded to agents-usage (README "Usage forwarding")."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from claude_code_wrapper.config import Config, UsageConfig
from claude_code_wrapper.internal_api import parse_statusline
from claude_code_wrapper.manager import CreateSessionRequest
from claude_code_wrapper.usage_forward import UsageForwarder

TOKEN = "usage-ingest-token-0123456789"
RL = {
    "five_hour": {"used_percentage": 23.5, "resets_at": 1794020400},
    "seven_day": {"used_percentage": 22, "resets_at": 1794049200},
}
LOGGER = "claude_code_wrapper.usage_forward"


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def cfg(**kw: Any) -> UsageConfig:
    return UsageConfig(**{"url": "http://usage.test", "token": TOKEN, "agent": "pod-0", **kw})


def changed(pct: float) -> dict[str, Any]:
    return {**RL, "five_hour": {**RL["five_hour"], "used_percentage": pct}}


class Fake:
    """An agents-usage stand-in: records requests, answers from a script (default 202)."""

    def __init__(self, *answers: int | type[Exception]) -> None:
        self.requests: list[httpx.Request] = []
        self.answers = list(answers)
        self.gate: asyncio.Event | None = None
        self.in_flight = 0
        self.max_in_flight = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.gate is not None:
                await self.gate.wait()
        finally:
            self.in_flight -= 1
        answer = self.answers.pop(0) if self.answers else 202
        if isinstance(answer, type):
            raise answer("boom", request=request)
        return httpx.Response(answer, json={"windows": 2})

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


async def until(pred: Callable[[], Any], timeout: float = 2.0) -> None:
    end = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < end, "timed out"
        await asyncio.sleep(0.005)


async def settle() -> None:
    """Let the sender finish handling the response it just got."""
    await asyncio.sleep(0.03)


async def running(fake: Fake, clock: Clock | None = None, **kw: Any) -> UsageForwarder:
    fwd = UsageForwarder(cfg(**kw), transport=httpx.MockTransport(fake), clock=clock or Clock())
    asyncio.get_running_loop().create_task(fwd.run())
    await asyncio.sleep(0)
    return fwd


async def test_posts_body_and_bearer() -> None:
    fake = Fake()
    fwd = await running(fake)
    try:
        fwd.offer(RL, "2.1.304")
        await until(lambda: len(fake.requests) == 1)
        (req,) = fake.requests
        assert req.method == "POST"
        assert str(req.url) == "http://usage.test/usage/v1/samples"
        assert req.headers["authorization"] == f"Bearer {TOKEN}"
        assert json.loads(req.content) == {
            "agent": "pod-0",
            "cli_version": "2.1.304",
            "rate_limits": RL,
        }
    finally:
        await fwd.aclose()


async def test_empty_agent_sent_as_null() -> None:
    fake = Fake()
    fwd = await running(fake, agent="")
    try:
        fwd.offer(RL, None)
        await until(lambda: len(fake.requests) == 1)
        assert fake.bodies() == [{"agent": None, "cli_version": None, "rate_limits": RL}]
    finally:
        await fwd.aclose()


async def test_unchanged_value_deduped_60s() -> None:
    fake, clock = Fake(), Clock()
    fwd = await running(fake, clock)
    try:
        fwd.offer(RL, "2.1.304")
        await until(lambda: len(fake.requests) == 1)
        await settle()
        for t in (10, 59):
            clock.t = t
            fwd.offer(RL, "2.1.304")
            await settle()
        assert len(fake.requests) == 1
        clock.t = 61
        fwd.offer(RL, "2.1.304")
        await until(lambda: len(fake.requests) == 2)
    finally:
        await fwd.aclose()


async def test_changed_value_sent_at_once() -> None:
    fake, clock = Fake(), Clock()
    fwd = await running(fake, clock)
    try:
        fwd.offer(RL, "2.1.304")
        await until(lambda: len(fake.requests) == 1)
        await settle()
        clock.t = 1
        fwd.offer(changed(24.0), "2.1.304")
        await until(lambda: len(fake.requests) == 2)
        assert fake.bodies()[1]["rate_limits"]["five_hour"]["used_percentage"] == 24.0
    finally:
        await fwd.aclose()


async def test_offer_never_blocks() -> None:
    fake = Fake()
    fake.gate = asyncio.Event()
    fwd = await running(fake)
    try:
        fwd.offer(changed(0), "2.1.304")
        await until(lambda: len(fake.requests) == 1)
        start = time.monotonic()
        for i in range(1, 1001):
            fwd.offer(changed(float(i)), "2.1.304")
        assert time.monotonic() - start < 0.1
        await settle()
        assert len(fake.requests) == 1 and fake.in_flight == 1
        fake.gate.set()
        await until(lambda: len(fake.requests) == 2)
        await settle()
        assert len(fake.requests) == 2
        assert fake.max_in_flight == 1
        assert fake.bodies()[1]["rate_limits"] == changed(1000.0)
    finally:
        await fwd.aclose()


async def test_return_to_last_sent_while_another_in_flight() -> None:
    fake, clock = Fake(), Clock()
    fwd = await running(fake, clock)
    try:
        fwd.offer(RL, "2.1.304")
        await until(lambda: len(fake.requests) == 1)
        await settle()
        fake.gate = asyncio.Event()
        clock.t = 1
        fwd.offer(changed(24.0), "2.1.304")
        await until(lambda: fake.in_flight == 1)
        fwd.offer(RL, "2.1.304")  # the server is about to hold 24.0, so RL must follow
        fake.gate.set()
        await until(lambda: len(fake.requests) == 3)
        assert fake.bodies()[2]["rate_limits"] == RL
    finally:
        await fwd.aclose()


def _forward_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]


async def test_failure_logged_once_per_streak(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    fake = Fake(500, 500, httpx.ConnectError, 202, 500)
    fwd = await running(fake)
    try:
        for n in range(1, 4):
            fwd.offer(RL, "2.1.304")
            await until(lambda n=n: len(fake.requests) == n)
            await settle()
        assert len(_forward_warnings(caplog)) == 1
        fwd.offer(RL, "2.1.304")  # 202 ends the streak
        await until(lambda: len(fake.requests) == 4)
        await settle()
        fwd.offer(changed(30.0), "2.1.304")  # a new failure starts a new streak
        await until(lambda: len(fake.requests) == 5)
        await settle()
        warnings = _forward_warnings(caplog)
        assert len(warnings) == 2
        assert "500" in warnings[0].getMessage() and "500" in warnings[1].getMessage()
    finally:
        await fwd.aclose()
    for record in caplog.records:
        dumped = f"{record.getMessage()} {record.msg} {record.args}"
        assert TOKEN not in dumped
        assert "23.5" not in dumped
        assert "30.0" not in dumped


async def test_exception_class_logged_not_message(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER)
    fake = Fake(httpx.ConnectError)
    fwd = await running(fake)
    try:
        fwd.offer(RL, "2.1.304")
        await until(lambda: len(fake.requests) == 1)
        await settle()
    finally:
        await fwd.aclose()
    (warning,) = _forward_warnings(caplog)
    assert "ConnectError" in warning.getMessage()
    assert "boom" not in warning.getMessage()


async def test_failed_post_retried_on_next_offer() -> None:
    fake, clock = Fake(500), Clock()
    fwd = await running(fake, clock)
    try:
        fwd.offer(RL, "2.1.304")
        await until(lambda: len(fake.requests) == 1)
        await settle()
        clock.t = 1
        fwd.offer(RL, "2.1.304")
        await until(lambda: len(fake.requests) == 2)
        await settle()
        clock.t = 2
        fwd.offer(RL, "2.1.304")  # the retry succeeded: now deduped
        await settle()
        assert len(fake.requests) == 2
    finally:
        await fwd.aclose()


async def test_sender_has_no_retry_loop() -> None:
    fake = Fake(500, 500, 500)
    fwd = await running(fake)
    try:
        fwd.offer(RL, "2.1.304")
        await until(lambda: len(fake.requests) == 1)
        await asyncio.sleep(0.2)
        assert len(fake.requests) == 1
    finally:
        await fwd.aclose()


async def test_offer_never_raises() -> None:
    fwd = UsageForwarder(cfg(), transport=httpx.MockTransport(Fake()))
    try:
        bad: Any = None
        fwd.offer(bad, "x")
        fwd.offer({"a": object()}, None)
    finally:
        await fwd.aclose()
    fwd.offer(RL, "2.1.304")  # after aclose: ignored, no error


async def test_aclose_stops_sender() -> None:
    fake = Fake()
    fake.gate = asyncio.Event()
    fwd = UsageForwarder(cfg(), transport=httpx.MockTransport(fake))
    task = asyncio.get_running_loop().create_task(fwd.run())
    fwd.offer(RL, "2.1.304")
    await until(lambda: fake.in_flight == 1)
    start = time.monotonic()
    await asyncio.wait_for(fwd.aclose(), 1)
    assert time.monotonic() - start < 1
    await asyncio.wait_for(asyncio.wait({task}), 1)
    assert task.done()
    assert fwd._client.is_closed


async def test_aclose_before_run_started() -> None:
    fwd = UsageForwarder(cfg(), transport=httpx.MockTransport(Fake()))
    task = asyncio.get_running_loop().create_task(fwd.run())
    await fwd.aclose()
    await asyncio.wait_for(asyncio.wait({task}), 1)
    assert task.done() and fwd._client.is_closed


# ---------------------------------------------------------------- service and manager wiring


class _Recorder:
    """Stands in for UsageForwarder in the service lifespan."""

    made: list[_Recorder] = []

    def __init__(self, cfg: UsageConfig, **kw: Any) -> None:
        self.cfg = cfg
        self.ran = asyncio.Event()
        self.closed = False
        _Recorder.made.append(self)

    def offer(self, rate_limits: Any, cli_version: Any) -> None:
        pass

    async def run(self) -> None:
        self.task = asyncio.current_task()
        self.ran.set()
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True
        self.task.cancel()


def _service_cfg(tmp_path: Path, **usage: Any) -> Config:
    return Config.model_validate(
        {
            "server": {
                "host": "127.0.0.1",
                "port": 0,
                "internal_port": 0,
                "auth_token": "tok-abcdefgh",
            },
            "paths": {
                "workdir_root": str(tmp_path / "wd"),
                "sessions_root": str(tmp_path / "ss"),
            },
            "limits": {"shutdown_timeout_seconds": 2},
            "usage": usage,
        }
    )


async def _lifespan(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> tuple[list[_Recorder], list[Any]]:
    from claude_code_wrapper import service

    _Recorder.made = []
    managers: list[Any] = []
    real = service.SessionManager

    def spy(*a: Any, **kw: Any) -> Any:
        m = real(*a, **kw)
        managers.append(m)
        return m

    monkeypatch.setattr(service, "UsageForwarder", _Recorder)
    monkeypatch.setattr(service, "SessionManager", spy)
    ready, stop = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(service.run_service(cfg, ready=ready, stop=stop))
    await asyncio.wait_for(ready.wait(), 15)
    for rec in _Recorder.made:
        await asyncio.wait_for(rec.ran.wait(), 2)
    stop.set()
    await asyncio.wait_for(task, 10)
    return _Recorder.made, managers


async def test_disabled_without_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    made, managers = await _lifespan(monkeypatch, _service_cfg(tmp_path))
    assert made == []
    assert managers[0].usage is None


async def test_lifespan_starts_and_closes_forwarder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _service_cfg(tmp_path, url="http://usage.test", token=TOKEN, agent="pod-0")
    made, managers = await _lifespan(monkeypatch, cfg)
    (rec,) = made
    assert rec.cfg == cfg.usage
    assert managers[0].usage is rec
    assert rec.ran.is_set() and rec.closed


async def test_manager_wires_router_to_forwarder(world: Any) -> None:
    fake = Fake()
    fwd = UsageForwarder(cfg(), transport=httpx.MockTransport(fake))
    asyncio.get_running_loop().create_task(fwd.run())
    m = world.manager(world.cfg(), usage=fwd)
    try:
        snap = await m.create(CreateSessionRequest())
        router = m.router(snap["session_id"])
        router.on_statusline(
            parse_statusline({"rate_limits": RL, "version": "2.1.304", "context_window": None})
        )
        await until(lambda: len(fake.requests) == 1)
        assert fake.bodies()[0]["rate_limits"] == RL
        router.on_statusline(parse_statusline({"context_window": None}))
        await settle()
        assert len(fake.requests) == 1
    finally:
        await m.shutdown()
        await fwd.aclose()
