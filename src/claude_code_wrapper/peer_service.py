"""The instance-wide peer service (part `peer-service`, design D1, D4, D7, D8, D9, D16).

One object per ccw instance runs membership (join, heartbeats, leave), signed sending for the
ccw MCP tools (`mcp_server.PeerTools`), receiving for the peer routes (`api.peers.PeerReceiver`)
and the delivery notices of every registered session. The peer token is a signing key only:
it is added to the redactor at construction and never logged, emitted or sent.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import ValidationError

from .config import PeersConfig
from .hooks import HookDispatcher
from .metrics import Metrics
from .peer_client import MemberView, MessageBody, NoticeBody, PeerClient, PeerError, new_msg_id
from .peer_inbox import InboxItem, OutcomeStatus, PeerInbox
from .peer_membership import MembershipService
from .peers import (
    Member,
    MemberTable,
    PeerSession,
    format_address,
    parse_address,
    resolve_advertise_url,
    resolve_instance_name,
)
from .redact import Redactor
from .turns import TurnRecord

if TYPE_CHECKING:
    from .router import SessionRouter

log = logging.getLogger(__name__)

#: NoticeBody.reason is capped on the wire.
_REASON_MAX = 200
#: The reason a queued message reports when its session closes before delivering it.
DROPPED_REASON = "session closed"


@dataclass
class _Local:
    """One session of this instance that takes part in peer messaging."""

    session_id: str
    router: SessionRouter
    inbox: PeerInbox
    name: str
    cwd: str


class PeerService:
    """Membership plus messaging for every session of this instance."""

    def __init__(
        self,
        cfg: PeersConfig,
        *,
        public_port: int,
        redactor: Redactor,
        dispatcher: HookDispatcher,
        metrics: Metrics | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        redactor.add([cfg.token])
        self.cfg = cfg
        self._redactor = redactor
        self._metrics = metrics
        self.instance = resolve_instance_name(cfg.name)
        self.url = resolve_advertise_url(cfg.advertise_url, public_port)
        self.client = PeerClient(cfg.token, transport=transport)
        self.table = MemberTable(self.instance, self.url, max_missed=cfg.max_missed_heartbeats)
        self._sessions: dict[str, _Local] = {}
        #: Every inbox ever registered and still alive: `stop` drains their outcome tasks,
        #: including those of sessions already unregistered (their dropped items' notices).
        self._inboxes: weakref.WeakSet[PeerInbox] = weakref.WeakSet()
        self._joined = False
        # The membership service seeds the table with cfg.static (seed contacts).
        self.membership = MembershipService(
            cfg,
            instance=self.instance,
            url=self.url,
            client=self.client,
            table=self.table,
            dispatcher=dispatcher,
            own_sessions=self._own_sessions,
            on_change=self._member_changed,
            metrics=metrics,
        )

    # ------------------------------------------------------------------ lifecycle

    async def join(self) -> dict[str, Any]:
        self._joined = True
        return await self.membership.join()

    async def leave(self) -> dict[str, Any]:
        return await self.membership.leave()

    async def stop(self, drain_timeout: float) -> None:
        """Drain every inbox's outcome tasks within `drain_timeout`, then close the client.

        The manager calls `leave()` first; when it has not, the leave runs here so the
        heartbeat loop never outlives the client.
        """
        if self._joined:
            await self.membership.leave()
        inboxes = list(self._inboxes)
        if inboxes:
            await asyncio.gather(*(inbox.drain(drain_timeout) for inbox in inboxes))
        await self.client.aclose()

    # ------------------------------------------------------------------ sessions

    def register(self, session_id: str, router: SessionRouter, *, name: str, cwd: str) -> PeerInbox:
        inbox = PeerInbox(
            router,
            max_items=self.cfg.inbox_max,
            max_per_minute=self.cfg.max_messages_per_minute,
            metrics=self._metrics,
            on_outcome=functools.partial(self._notify, session_id=session_id, router=router),
            redactor=self._redactor,
        )
        router.peer_inbox = inbox
        self._sessions[session_id] = _Local(session_id, router, inbox, name, cwd)
        self._inboxes.add(inbox)
        self.membership.sessions_changed()
        return inbox

    def unregister(self, session_id: str) -> None:
        if self._sessions.pop(session_id, None) is not None:
            self.membership.sessions_changed()

    def _own_sessions(self) -> list[PeerSession]:
        return [PeerSession(s.session_id, s.name, s.cwd) for s in self._sessions.values()]

    def _member_changed(self, kind: str, member: Member, reason: str) -> None:
        data = {"instance": member.instance, "url": member.url, "reason": reason}
        for local in list(self._sessions.values()):
            try:
                local.router.emit_event(f"peer_member_{kind}", data)
            except Exception:  # noqa: BLE001 - one session's log never breaks membership
                log.exception("peer: emitting peer_member_%s failed", kind)

    # ------------------------------------------------------------------ addressing

    def _address(self, session_id: str) -> str:
        return format_address(self.instance, session_id)

    def _resolve(self, address: str) -> str | None:
        """The url serving `address`: ours for a registered local session, else a member's."""
        instance, session_id = parse_address(address)
        if instance == self.instance:
            return self.url if session_id in self._sessions else None
        return self.table.lookup(address)

    def _count(self, kind: str, result: str) -> None:
        if self._metrics is not None:
            self._metrics.peer_sent(kind, result)

    def _emit_sent(self, router: SessionRouter | None, data: dict[str, Any]) -> None:
        if router is None:
            return
        try:
            router.emit_event("peer_message_sent", data)
        except Exception:  # noqa: BLE001 - a log failure never fails a send
            log.exception("peer: emitting peer_message_sent failed")

    # ------------------------------------------------------------------ PeerTools

    async def list_peers(self, caller_session: str, instance: str | None) -> list[dict]:
        caller = self._address(caller_session)
        rows = []
        if instance is None or instance == self.instance:
            rows = [
                {
                    "address": self._address(s.session_id),
                    "instance": self.instance,
                    "session_id": s.session_id,
                    "name": s.name,
                    "cwd": s.cwd,
                }
                for s in self._sessions.values()
                if s.session_id != caller_session
            ]
        rows += self.table.sessions(exclude=caller, instance=instance)
        return sorted(rows, key=lambda row: row["address"])

    async def send(self, caller_session: str, to: str, message: str, notify_when_idle: bool) -> str:
        """Send `message` to the session at `to`; returns its msg_id or raises PeerError."""
        msg_id = new_msg_id()
        local = self._sessions.get(caller_session)
        router = local.router if local is not None else None
        event = {
            "msg_id": msg_id,
            "to": to,
            "kind": "message",
            "bytes": len(message.encode("utf-8")),
            "notify_when_idle": notify_when_idle,
        }
        try:
            await self._send(caller_session, to, message, notify_when_idle, msg_id)
        except PeerError as exc:
            self._count("message", exc.code)
            self._emit_sent(router, {**event, "result": exc.code})
            raise
        self._count("message", "ok")
        self._emit_sent(router, {**event, "result": "ok"})
        return msg_id

    async def _send(
        self, caller_session: str, to: str, message: str, notify_when_idle: bool, msg_id: str
    ) -> None:
        caller = self._address(caller_session)
        try:
            _, to_session = parse_address(to)
        except ValueError:
            raise PeerError(
                "invalid", "invalid arguments: `to` must be <instance>/<session id>"
            ) from None
        if to == caller:
            raise PeerError("invalid", "invalid arguments: a session cannot message itself")
        if not message:
            raise PeerError("invalid", "invalid arguments: `message` must not be empty")
        size = len(message.encode("utf-8"))
        if size > self.cfg.max_message_bytes:
            raise PeerError(
                "too_large",
                f"message too large: {size} bytes, at most {self.cfg.max_message_bytes}",
            )
        url = self._resolve(to)
        if url is None:
            raise PeerError("unknown_peer", f"unknown peer: {to} is not a reachable session")
        body = MessageBody.model_validate(
            {
                "from": caller,
                "to_session": to_session,
                "message": message,
                "msg_id": msg_id,
                "notify_when_idle": notify_when_idle,
            }
        )
        await self.client.send_message(url, body)

    # ------------------------------------------------------------------ PeerReceiver

    def _target(self, session_id: str) -> PeerInbox:
        local = self._sessions.get(session_id)
        if local is None or local.inbox.closed:
            raise PeerError("not_found", f"no running session {session_id} here")
        return local.inbox

    async def accept_message(self, body: MessageBody) -> dict[str, Any]:
        inbox = self._target(body.to_session)
        put = inbox.put(
            InboxItem(
                kind="message",
                from_address=body.from_,
                msg_id=body.msg_id,
                text=body.message,
                notify_when_idle=body.notify_when_idle,
            )
        )
        return {"msg_id": body.msg_id, "queued": put.depth, "duplicate": put.duplicate}

    async def accept_notice(self, body: NoticeBody) -> dict[str, Any]:
        inbox = self._target(body.to_session)
        put = inbox.put(
            InboxItem(
                kind="notice",
                from_address=body.from_,
                msg_id=body.msg_id,
                text="",
                status=body.status,
                state=body.state,
                reason=body.reason,
            )
        )
        return {"msg_id": body.msg_id, "queued": put.depth, "duplicate": put.duplicate}

    async def accept_heartbeat(self, view: MemberView) -> MemberView:
        return await self.membership.accept_heartbeat(view)

    # ------------------------------------------------------------------ notices (D8)

    async def _notify(
        self,
        item: InboxItem,
        status: OutcomeStatus,
        record: TurnRecord | None,
        *,
        session_id: str,
        router: SessionRouter,
    ) -> None:
        """Tell the sender of `item` what became of it. Logged and counted, never raised.

        Bound per session at `register`, so it still runs for the items dropped when an
        unregistered session's router closes its inbox.
        """
        if status == "delivered":
            state, reason = (record.state if record is not None else None), None
        else:
            state, reason = None, (item.reason or DROPPED_REASON)[:_REASON_MAX]
        sender = item.from_address
        event = {
            "msg_id": item.msg_id,
            "to": sender,
            "kind": "notice",
            "bytes": 0,
            "notify_when_idle": False,
        }
        try:
            url = self._resolve(sender)
        except ValueError:
            url = None
        try:
            if url is None:
                raise PeerError("unknown_peer", f"unknown peer: {sender} is not reachable")
            try:
                body = NoticeBody.model_validate(
                    {
                        "from": self._address(session_id),
                        "to_session": parse_address(sender)[1],
                        "msg_id": item.msg_id,
                        "status": status,
                        "state": state,
                        "reason": reason,
                    }
                )
            except ValidationError as exc:
                raise PeerError("invalid", f"invalid notice: {exc.error_count()} errors") from None
            await self.client.send_notice(url, body)
        except PeerError as exc:
            log.warning(
                "peer: %s notice for %s to %s not sent: %s",
                status,
                item.msg_id,
                self._redactor.redact(sender),
                self._redactor.redact(exc.message),
            )
            self._count("notice", exc.code)
            self._emit_sent(router, {**event, "result": exc.code})
            return
        self._count("notice", "ok")
        self._emit_sent(router, {**event, "result": "ok"})
