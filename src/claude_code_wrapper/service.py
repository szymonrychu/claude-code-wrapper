"""Process entry point: logging, global mise, the two uvicorn servers and bounded shutdown."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import uvicorn

from claude_code_wrapper import tooling
from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.config import Config
from claude_code_wrapper.internal_api import create_internal_app
from claude_code_wrapper.manager import SessionManager
from claude_code_wrapper.metrics import Metrics
from claude_code_wrapper.redact import Redactor

log = logging.getLogger(__name__)

INTERNAL_HOST = "127.0.0.1"
GRACEFUL_SHUTDOWN_SECONDS = 3
#: Delay between an exit request and the stop, so the triggering response is sent first.
EXIT_STOP_DELAY_SECONDS = 0.2
STARTUP_TIMEOUT_SECONDS = 30
_STD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
    "color_message",
}


@dataclass
class ServiceHandle:
    """The ports the service actually bound (filled before `ready` is set)."""

    public_port: int = 0
    internal_port: int = 0
    exit_code: int | None = None  # set by the first exit request (sessions.exit_when_*)

    def request_exit(self, code: int) -> bool:
        """Record `code` unless an earlier request already did; True when this one won."""
        if self.exit_code is not None:
            return False
        self.exit_code = code
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line: `ts, level, logger, msg` plus any `extra=` fields."""

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and key not in out:
                out[key] = value
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


class RedactingFilter(logging.Filter):
    """Redacts the message, its args and every extra field in place."""

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        red = self._redactor
        record.msg = red.redact(record.getMessage()) if record.args else red.redact(record.msg)
        record.args = None
        for key, value in list(record.__dict__.items()):
            if key not in _STD_ATTRS:
                record.__dict__[key] = red.redact_obj(value)
        if record.exc_info:
            record.exc_text = red.redact(self.formatter_text(record))
            record.exc_info = None
        return True

    @staticmethod
    def formatter_text(record: logging.LogRecord) -> str:
        return logging.Formatter().formatException(record.exc_info)  # type: ignore[arg-type]


@contextlib.contextmanager
def _logging(level: str, redactor: Redactor) -> Iterator[None]:
    root = logging.getLogger()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter(redactor))
    saved = (root.level, list(root.handlers))
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    try:
        yield
    finally:
        root.handlers[:] = saved[1]
        root.setLevel(saved[0])


class _Server(uvicorn.Server):
    """A uvicorn server that leaves signal handling to `run_service`."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def _bind(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(128)
        sock.setblocking(False)
    except OSError:
        sock.close()
        raise
    return sock


def _server(app: Any, level: str) -> _Server:
    config = uvicorn.Config(
        app,
        log_config=None,
        log_level=level,
        access_log=False,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        lifespan="off",
    )
    return _Server(config)


@contextlib.contextmanager
def _stop_on_signals(stop: asyncio.Event) -> Iterator[None]:
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (RuntimeError, ValueError):  # includes NotImplemented
            continue  # not the main thread, or no signal support
        installed.append(sig)
    try:
        yield
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


async def run_service(
    cfg: Config,
    *,
    ready: asyncio.Event | None = None,
    stop: asyncio.Event | None = None,
    handle: ServiceHandle | None = None,
) -> None:
    """Run the public and internal servers until `stop` is set or SIGTERM/SIGINT arrives.
    An exit policy request leaves its code on `handle.exit_code`.

    Raises `tooling.ToolingError` when a required global mise step fails, and `OSError` when a
    port cannot be bound."""
    redactor = Redactor()
    stop = stop if stop is not None else asyncio.Event()
    handle = handle if handle is not None else ServiceHandle()
    with _logging(cfg.server.log_level, redactor), _stop_on_signals(stop):
        await _run(cfg, redactor, ready, stop, handle)


async def _run(
    cfg: Config,
    redactor: Redactor,
    ready: asyncio.Event | None,
    stop: asyncio.Event,
    handle: ServiceHandle,
) -> None:
    tooling.render_global_mise(cfg.mise)
    await tooling.install_global(cfg.mise, tooling.mise_env(cfg.mise))

    socks = [
        _bind(cfg.server.host, cfg.server.port),
        _bind(INTERNAL_HOST, cfg.server.internal_port),
    ]
    servers: list[_Server] = []
    tasks: list[asyncio.Task[None]] = []
    try:
        handle.public_port = socks[0].getsockname()[1]
        handle.internal_port = socks[1].getsockname()[1]
        metrics = Metrics()

        def on_exit_request(code: int) -> None:
            if handle.request_exit(code):
                asyncio.get_running_loop().call_later(EXIT_STOP_DELAY_SECONDS, stop.set)

        manager = SessionManager(
            cfg,
            internal_url=f"http://{INTERNAL_HOST}:{handle.internal_port}",
            service_env=dict(os.environ),
            api_url=f"http://127.0.0.1:{handle.public_port}",
            redactor=redactor,
            metrics=metrics,
            on_exit_request=on_exit_request,
        )
        await manager.load_persisted()
        level = cfg.server.log_level
        servers = [
            _server(create_app(manager, cfg, metrics), level),
            _server(create_internal_app(manager), level),
        ]
        tasks = [
            asyncio.create_task(srv.serve(sockets=[sock]))
            for srv, sock in zip(servers, socks, strict=True)
        ]
        await _wait_listening(servers, tasks)
        manager.started = True
        log.info(
            "listening",
            extra={"public_port": handle.public_port, "internal_port": handle.internal_port},
        )
        if ready is not None:
            ready.set()
        stopper = asyncio.create_task(stop.wait())
        await asyncio.wait({stopper, *tasks}, return_when=asyncio.FIRST_COMPLETED)
        stopper.cancel()
        log.info("shutting down")
        manager.started = False
        await manager.shutdown()
        log.info("shutdown complete")
    finally:
        for srv in servers:
            srv.should_exit = True
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for sock in socks:
            sock.close()


async def _wait_listening(servers: list[_Server], tasks: list[asyncio.Task[None]]) -> None:
    deadline = asyncio.get_running_loop().time() + STARTUP_TIMEOUT_SECONDS
    while not all(s.started for s in servers):
        for task in tasks:
            if task.done():
                task.result()
                raise RuntimeError("server exited during startup")
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("servers did not start listening")
        await asyncio.sleep(0.01)
