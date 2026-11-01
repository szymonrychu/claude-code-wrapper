"""Forward the agents' statusLine `rate_limits` to agents-usage (README "Usage forwarding").

One forwarder per service process. `offer` is synchronous and cheap, so the statusLine route
never waits on the network: it drops a value equal to the last one sent (unless
`min_interval_seconds` passed since that send) and otherwise puts the value in a one-slot,
latest-wins buffer. One sender task POSTs the slot; a slow or dead agents-usage never builds a
backlog. A failed POST is retried only by the next offer. Logs carry a status code or an
exception class only, never the body, the token or a percent.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import math
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from claude_code_wrapper.config import UsageConfig

log = logging.getLogger(__name__)

SAMPLES_PATH = "/usage/v1/samples"

#: (rate_limits, cli_version): what is sent, and what an offer is compared with.
_Value = tuple[dict[str, Any], str | None]


class UsageForwarder:
    def __init__(
        self,
        cfg: UsageConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cfg = cfg
        self._clock = clock
        self._url = cfg.url + SAMPLES_PATH
        self._client = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            timeout=cfg.timeout_seconds,
            headers={"Authorization": f"Bearer {cfg.token}"},
        )
        self._pending: _Value | None = None
        self._wake = asyncio.Event()
        self._last_sent: _Value | None = None
        #: The value being POSTed right now; the server holds it once the POST succeeds.
        self._in_flight: _Value | None = None
        self._last_sent_at = -math.inf
        self._failing = False
        self._closed = False
        self._task: asyncio.Task[Any] | None = None

    def offer(self, rate_limits: Mapping[str, Any], cli_version: str | None) -> None:
        """Queue `rate_limits` for the sender unless it repeats the last send too soon."""
        try:
            if self._closed:
                return
            value: _Value = (copy.deepcopy(dict(rate_limits)), cli_version)
            if (
                value == self._last_sent
                and self._in_flight in (None, value)
                and self._clock() - self._last_sent_at < self._cfg.min_interval_seconds
            ):
                self._pending = None  # the latest value is already on the server
                return
            self._pending = value
            self._wake.set()
        except Exception:
            log.debug("usage offer dropped")

    async def run(self) -> None:
        """The single sender loop; ends when `aclose` cancels it."""
        if self._closed:
            return
        self._task = asyncio.current_task()
        while True:
            await self._wake.wait()
            self._wake.clear()
            value, self._pending = self._pending, None
            if value is None:
                continue
            self._in_flight = value
            try:
                await self._send(value)
            finally:
                self._in_flight = None

    async def _send(self, value: _Value) -> None:
        rate_limits, cli_version = value
        body = {
            "agent": self._cfg.agent or None,
            "cli_version": cli_version,
            "rate_limits": rate_limits,
        }
        try:
            response = await self._client.post(self._url, json=body)
        except Exception as exc:
            self._failed(type(exc).__name__)
            return
        if not response.is_success:
            self._failed(f"status {response.status_code}")
            return
        self._last_sent, self._last_sent_at = value, self._clock()
        if self._failing:
            self._failing = False
            log.info("usage forward recovered")

    def _failed(self, reason: str) -> None:
        if not self._failing:
            self._failing = True
            log.warning("usage forward failed: %s", reason)

    async def aclose(self) -> None:
        """Stop the sender (an in-flight POST is cancelled) and close the HTTP client."""
        self._closed = True
        self._pending = None
        task = self._task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.wait({task})
        await self._client.aclose()
