"""Peer member table and addressing. Pure and synchronous; no I/O."""

from __future__ import annotations

import re
import socket
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

_INSTANCE_PATTERN = r"[A-Za-z0-9._-]{1,64}"
_SESSION_PATTERN = r"ses_[0-9a-f]{12}"
ADDRESS_RE = re.compile(rf"^({_INSTANCE_PATTERN})/({_SESSION_PATTERN})$")
_INSTANCE_BAD_CHARS = re.compile(r"[^A-Za-z0-9._-]")

FORGET_FACTOR = 10


class TableFull(Exception):
    """A new member would exceed the table capacity."""


def _valid_instance(name: str) -> bool:
    return bool(name) and name not in (".", "..") and not name.startswith(".")


def parse_address(address: str) -> tuple[str, str]:
    """Split `<instance>/<session id>`; raise ValueError when malformed."""
    m = ADDRESS_RE.match(address) if isinstance(address, str) else None
    if m is None or not _valid_instance(m.group(1)):
        raise ValueError(f"bad peer address: {address!r}")
    return m.group(1), m.group(2)


def format_address(instance: str, session_id: str) -> str:
    return f"{instance}/{session_id}"


def resolve_instance_name(name: str) -> str:
    if name:
        return name
    cleaned = _INSTANCE_BAD_CHARS.sub("", socket.gethostname()).lstrip(".")[:64]
    return cleaned or "ccw"


def resolve_advertise_url(advertise_url: str, port: int) -> str:
    if not advertise_url:
        return f"http://{socket.gethostname()}:{port}"
    return advertise_url.replace("{port}", str(port))


@dataclass(frozen=True)
class PeerSession:
    session_id: str
    name: str
    cwd: str


@dataclass
class Member:
    instance: str
    url: str
    sessions: tuple[PeerSession, ...] = ()
    reachable: bool = True
    missed: int = 0
    last_seen: float | None = None


class MemberTable:
    def __init__(
        self,
        self_instance: str,
        self_url: str,
        *,
        max_missed: int,
        capacity: int = 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.self_instance = self_instance
        self.self_url = self_url
        self.max_missed = max_missed
        self.capacity = capacity
        self._clock = clock
        self._members: dict[str, Member] = {}
        self._contacts: dict[str, None] = {}  # ordered set of urls
        self._seeds: set[str] = set()
        self._answered: set[str] = set()  # urls that ever answered a heartbeat
        self._fails: dict[str, int] = {}

    def add_contacts(self, urls: Iterable[str], seed: bool = False) -> None:
        for url in urls:
            if not url or url == self.self_url:
                continue
            if seed:
                self._seeds.add(url)
            self._add_contact(url, force=seed)

    def _add_contact(self, url: str, *, force: bool = False) -> None:
        """Remember `url`; `force` (seeds, answering peers) may evict unconfirmed gossip."""
        if url in self._contacts:
            return
        if len(self._contacts) >= self.capacity:
            if not force:
                return
            victim = next(
                (u for u in self._contacts if u not in self._answered and u not in self._seeds),
                None,
            )
            if victim is not None:
                self._forget_contact(victim)
            elif url not in self._seeds:
                return
        self._contacts[url] = None

    def _forget_contact(self, url: str) -> None:
        self._contacts.pop(url, None)
        self._answered.discard(url)
        self._fails.pop(url, None)

    def targets(self) -> list[str]:
        urls = dict.fromkeys(self._contacts)
        urls.update(dict.fromkeys(m.url for m in self._members.values()))
        urls.pop(self.self_url, None)
        return list(urls)

    def record_report(
        self,
        instance: str,
        url: str,
        sessions: Iterable[PeerSession],
        members: Iterable[dict] | None = None,
    ) -> str | None:
        if instance == self.self_instance or url == self.self_url:
            return None
        member = self._members.get(instance)
        if member is None:
            # a url is owned by one instance name at a time
            for other in [m for m in self._members.values() if m.url == url]:
                del self._members[other.instance]
            if len(self._members) >= self.capacity:
                raise TableFull(f"peer table full ({self.capacity})")
            member = self._members[instance] = Member(instance, url, reachable=False)
        elif member.url != url:
            old = member.url
            member.url = url
            self._fails.pop(old, None)
            if old not in self._seeds:
                self._forget_contact(old)
        joined = not member.reachable
        member.sessions = tuple(sessions)
        member.reachable = True
        member.missed = 0
        member.last_seen = self._clock()
        self._fails.pop(url, None)
        self._answered.add(url)
        self._add_contact(url, force=True)
        self.add_contacts(m["url"] for m in members or () if m.get("url"))
        return "joined" if joined else None

    def _member_at(self, url: str) -> Member | None:
        for m in self._members.values():
            if m.url == url:
                return m
        return None

    def record_failure(self, url: str) -> str | None:
        fails = self._fails[url] = self._fails.get(url, 0) + 1
        event = None
        member = self._member_at(url)
        if member is not None:
            member.missed += 1
            if member.reachable and member.missed >= self.max_missed:
                member.reachable = False
                event = "missed_heartbeats"
        gossip_only = url not in self._answered and member is None
        limit = self.max_missed if gossip_only else FORGET_FACTOR * self.max_missed
        if fails >= limit and url not in self._seeds:
            self._forget_contact(url)
            if member is not None:
                del self._members[member.instance]
        return event

    def remove(self, instance_or_url: str) -> bool:
        member = self._members.get(instance_or_url) or self._member_at(instance_or_url)
        if member is None or not member.reachable:
            return False
        member.reachable = False
        member.sessions = ()
        self._add_contact(member.url, force=True)
        return True

    def view(self, own_sessions: Iterable[PeerSession]) -> dict:
        return {
            "instance": self.self_instance,
            "url": self.self_url,
            "sessions": [
                {"session_id": s.session_id, "name": s.name, "cwd": s.cwd} for s in own_sessions
            ],
            "members": [{"instance": m.instance, "url": m.url} for m in self.reachable()],
        }

    def snapshot(self) -> list[tuple[Member, float | None]]:
        """Every known member (reachable or not) with `last_seen` as wall-clock epoch seconds."""
        offset = time.time() - self._clock()
        return [
            (replace(m), None if m.last_seen is None else m.last_seen + offset)
            for m in self._members.values()
        ]

    def reachable(self) -> list[Member]:
        return [replace(m) for m in self._members.values() if m.reachable]

    def sessions(self, exclude: str | None = None, instance: str | None = None) -> list[dict]:
        out = []
        for m in self.reachable():
            if instance is not None and m.instance != instance:
                continue
            for s in m.sessions:
                address = format_address(m.instance, s.session_id)
                if address == exclude:
                    continue
                out.append(
                    {
                        "address": address,
                        "instance": m.instance,
                        "session_id": s.session_id,
                        "name": s.name,
                        "cwd": s.cwd,
                    }
                )
        return sorted(out, key=lambda d: d["address"])

    def lookup(self, address: str) -> str | None:
        try:
            instance, sid = parse_address(address)
        except ValueError:
            return None
        m = self._members.get(instance)
        if m is None or not m.reachable:
            return None
        return m.url if any(s.session_id == sid for s in m.sessions) else None
