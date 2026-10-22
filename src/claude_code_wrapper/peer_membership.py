"""Peer membership runtime: join, heartbeat and leave over the heartbeat route (design D5, D16).

The join is the first heartbeat round, framed by preJoin (may veto: standalone) and postJoin.
The leave is one final `leaving: true` heartbeat, framed by preLeave and postLeave. Every
outbound call runs concurrently under its own timeout, so no wait is longer than the hook
timeouts plus one call bound, whatever the peers do.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any, TypeVar

from .config import PeersConfig
from .hooks import HookDispatcher, fire_instance
from .metrics import Metrics
from .peer_client import MemberView, PeerClient, PeerError
from .peers import Member, MemberTable, PeerSession, TableFull

log = logging.getLogger(__name__)

T = TypeVar("T")
OnChange = Callable[[str, Member, str], None]


def _error_text(exc: BaseException, timeout: float) -> str:
    if isinstance(exc, PeerError):
        return exc.message
    if isinstance(exc, TimeoutError):
        return f"timeout after {timeout:g}s"
    return type(exc).__name__


class MembershipService:
    def __init__(
        self,
        cfg: PeersConfig,
        *,
        instance: str,
        url: str,
        client: PeerClient,
        table: MemberTable,
        dispatcher: HookDispatcher,
        own_sessions: Callable[[], list[PeerSession]],
        on_change: OnChange | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self._cfg = cfg
        self._instance = instance
        self._url = url
        self._client = client
        self._table = table
        self._dispatcher = dispatcher
        self._own_sessions = own_sessions
        self._on_change = on_change
        self._metrics = metrics
        self._standalone = False
        self._join_data: dict[str, Any] | None = None
        self._leaving = False
        self._leave_data: dict[str, Any] | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        table.add_contacts(cfg.static, seed=True)

    @property
    def standalone(self) -> bool:
        return self._standalone

    @property
    def table(self) -> MemberTable:
        return self._table

    # ------------------------------------------------------------------ helpers

    def _count(self, op: str, result: str) -> None:
        if self._metrics is not None:
            self._metrics.peer_membership(op, result)

    def _members(self) -> list[dict[str, str]]:
        return [{"instance": m.instance, "url": m.url} for m in self._table.reachable()]

    def _hook_data(self) -> dict[str, Any]:
        return {"instance": self._instance, "url": self._url, "members": self._members()}

    def _view(self, *, leaving: bool = False) -> MemberView:
        sessions = [] if leaving else self._own_sessions()
        return MemberView.model_validate({**self._table.view(sessions), "leaving": leaving})

    def _mutate(self, reason: str, change: Callable[[], T]) -> T:
        """Apply a table change and report every member entering or leaving the reachable list."""
        before = {m.instance: m for m in self._table.reachable()}
        try:
            return change()
        finally:
            after = {m.instance: m for m in self._table.reachable()}
            for name, member in before.items():
                if name not in after:
                    self._emit("left", replace(member, reachable=False, sessions=()), reason)
            for name, member in after.items():
                if name not in before:
                    self._emit("joined", member, reason)
            if self._metrics is not None:
                self._metrics.peer_members(len(after))

    def _emit(self, kind: str, member: Member, reason: str) -> None:
        log.info("peer %s %s (%s, %s)", member.instance, kind, member.url, reason)
        if self._on_change is None:
            return
        try:
            self._on_change(kind, member, reason)
        except Exception:  # noqa: BLE001 - a listener never breaks membership
            log.exception("peer membership listener failed")

    def _record(self, answer: MemberView, reason: str) -> None:
        sessions = [PeerSession(s.session_id, s.name, s.cwd) for s in answer.sessions]
        members = [m.model_dump() for m in answer.members]
        self._mutate(
            reason,
            lambda: self._table.record_report(answer.instance, answer.url, sessions, members),
        )

    async def _call(self, url: str, view: MemberView, timeout: float) -> MemberView:
        return await asyncio.wait_for(self._client.heartbeat(url, view), timeout)

    # ------------------------------------------------------------------ rounds

    async def _round(self, op: str, timeout: float) -> list[dict[str, str]]:
        """One heartbeat round to every target concurrently; returns the failures."""
        view = self._view()
        targets = self._table.targets()
        reason = "join" if op == "join" else "heartbeat"
        failures: list[dict[str, str]] = []

        async def one(url: str) -> None:
            try:
                answer = await self._call(url, view, timeout)
            except Exception as exc:  # noqa: BLE001 - every failure is a miss, never a crash
                if isinstance(exc, PeerError) and exc.code == "left":
                    # the peer has left: drop it now instead of counting misses
                    self._mutate("leave", lambda: self._table.remove(url))
                    self._count(op, "failed")
                    return
                error = _error_text(exc, timeout)
                log.debug("peer heartbeat to %s failed: %s", url, error)
                failures.append({"url": url, "error": error})
                self._count(op, "failed")
                if op == "heartbeat":
                    self._mutate("missed_heartbeats", lambda: self._table.record_failure(url))
                return
            try:
                self._record(answer, reason)
            except TableFull as exc:
                log.warning("peer %s not recorded: %s", answer.instance, exc)
                failures.append({"url": url, "error": str(exc)})
                self._count(op, "failed")
                return
            self._count(op, "ok")

        await asyncio.gather(*(one(url) for url in targets))
        if self._metrics is not None:
            self._metrics.peer_members(len(self._table.reachable()))
        return failures

    async def _heartbeat_loop(self) -> None:
        interval = self._cfg.heartbeat_seconds
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), interval)
            # cleared before the round: a change during the round queues exactly one more
            self._wake.clear()
            try:
                await self._round("heartbeat", interval)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop outlives any one bad round
                log.exception("peer heartbeat round failed")

    def sessions_changed(self) -> None:
        """Schedule one immediate heartbeat round; bursts coalesce. Never awaited."""
        if self._loop_task is not None and not self._leaving:
            self._wake.set()

    # ------------------------------------------------------------------ join / leave

    async def join(self) -> dict[str, Any]:
        if self._join_data is not None:
            return dict(self._join_data)
        outcome = await fire_instance(self._dispatcher, "preJoin", self._hook_data())
        failures: list[dict[str, str]] = []
        if outcome.decision == "deny":
            self._standalone = True
            self._count("join", "vetoed")
            log.warning("preJoin denied: %s runs standalone (%s)", self._instance, outcome.reason)
        else:
            failures = await self._round("join", self._cfg.join_timeout_seconds)
        data = {**self._hook_data(), "failures": failures, "standalone": self._standalone}
        self._join_data = data
        await fire_instance(self._dispatcher, "postJoin", data)
        if not self._standalone and not self._leaving:
            self._loop_task = asyncio.create_task(self._heartbeat_loop(), name="peer-heartbeat")
        return dict(data)

    async def leave(self) -> dict[str, Any]:
        if self._leaving:
            if self._leave_data is not None:
                return dict(self._leave_data)
            return {**self._hook_data(), "failures": []}
        self._leaving = True
        task, self._loop_task = self._loop_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await fire_instance(self._dispatcher, "preLeave", self._hook_data())
        members = self._members()
        failures: list[dict[str, str]] = []
        if not self._standalone:
            failures = await self._send_leaving([m["url"] for m in members])
        data = {"instance": self._instance, "url": self._url, "members": members}
        data["failures"] = failures
        self._leave_data = data
        await fire_instance(self._dispatcher, "postLeave", data)
        return dict(data)

    async def _send_leaving(self, urls: list[str]) -> list[dict[str, str]]:
        view = self._view(leaving=True)
        timeout = self._cfg.join_timeout_seconds
        failures: list[dict[str, str]] = []

        async def one(url: str) -> None:
            try:
                await self._call(url, view, timeout)
            except Exception as exc:  # noqa: BLE001 - a leave never fails the shutdown
                failures.append({"url": url, "error": _error_text(exc, timeout)})
                self._count("leave", "failed")
                return
            self._count("leave", "ok")

        await asyncio.gather(*(one(url) for url in urls))
        return failures

    # ------------------------------------------------------------------ receiver

    async def accept_heartbeat(self, view: MemberView) -> MemberView:
        """Record a member's own report and answer with our view (standalone included)."""
        if view.leaving:
            self._mutate("leave", lambda: self._table.remove(view.instance))
            return self._view()
        if self._leaving:
            raise PeerError("left", "this instance has left")
        try:
            self._record(view, "heartbeat")
        except TableFull:
            raise PeerError("rate_limited", "peer table full") from None
        return self._view()
