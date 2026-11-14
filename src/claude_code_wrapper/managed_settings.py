"""The managed-settings file ccw owns (spec D2): install at service start, Q3 fallback.

Claude Code reads `/etc/claude-code/managed-settings.json` as the top settings tier. ccw
writes the rendered baseline there (mode 0644) before any session starts, reads it back and
checks the PreToolUse and PermissionRequest relay. Any failure leaves state `fallback`: the
sessions' settings.json then keeps the relay for every event (Q3). Logs name the path and the
reason, never the file content.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from claude_code_wrapper.bootstrap.settings import MANAGED_RELAY_EVENTS, _is_relay

log = logging.getLogger(__name__)

ManagedState = Literal["active", "fallback", "degraded"]
TMP_NAME = ".managed-settings.json.ccw-tmp"
#: Names Claude Code itself reads beside the managed file; allowed in the managed dir (R1).
CLAUDE_MANAGED_NAMES = frozenset({"managed-settings.d", "managed-mcp.json"})


def serialize(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc, indent=2, sort_keys=True).encode() + b"\n"


def write_managed(path: Path, data: bytes) -> None:
    """Atomic write: a temp file in the same dir, fsync, `os.replace` (which replaces a symlink
    at `path` rather than following it), then mode 0644."""
    tmp = path.parent / TMP_NAME
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    os.chmod(path, 0o644)


def has_relay(doc: Any, relay: str) -> bool:
    """True when every `MANAGED_RELAY_EVENTS` event carries a relay hook."""
    hooks = doc.get("hooks") if isinstance(doc, dict) else None
    if not isinstance(hooks, dict):
        return False
    for event in MANAGED_RELAY_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list) or not any(
            _is_relay(h, relay)
            for g in groups
            if isinstance(g, dict) and isinstance(g.get("hooks"), list)
            for h in g["hooks"]
        ):
            return False
    return True


@dataclass
class ManagedStatus:
    state: ManagedState
    path: str
    sha256: str | None
    reason: str | None
    restores: int
    overlay: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ManagedSettings:
    """Owner of the managed-settings file. One lock serialises install, replace and check
    (ruling R2); `_expected` and `_sha256` are what the file must hold."""

    def __init__(self, path: Path, baseline: dict[str, Any], relay: str) -> None:
        self.path = Path(path)
        self.relay = relay
        self._baseline = copy.deepcopy(baseline)
        self._overlay: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._state: ManagedState = "fallback"
        self._reason: str | None = "not installed"
        self._restores = 0
        self._expected = serialize(self.document)
        self._sha256 = hashlib.sha256(self._expected).hexdigest()

    @property
    def document(self) -> dict[str, Any]:
        """What the file should hold."""
        return copy.deepcopy(self._baseline)

    @property
    def active(self) -> bool:
        return self._state == "active"

    def status(self) -> ManagedStatus:
        return ManagedStatus(
            state=self._state,
            path=str(self.path),
            sha256=self._sha256 if self._state != "fallback" else None,
            reason=self._reason,
            restores=self._restores,
            overlay=copy.deepcopy(self._overlay),
        )

    def install(self) -> ManagedStatus:
        """Write the baseline, read it back, check the relay: `active`, else `fallback`."""
        with self._lock:
            reason = self._install()
            if reason is None:
                self._state, self._reason = "active", None
            else:
                self._state, self._reason = "fallback", reason
                log.warning(
                    "managed settings %s: falling back to the settings.json relay: %s",
                    self.path,
                    reason,
                )
            return self.status()

    def _install(self) -> str | None:
        try:
            foreign = sorted(
                name
                for name in os.listdir(self.path.parent)
                if name not in {self.path.name, TMP_NAME, *CLAUDE_MANAGED_NAMES}
            )
            if foreign:
                return (
                    f"managed dir {self.path.parent} is not dedicated "
                    f"({len(foreign)} other entries)"
                )
            write_managed(self.path, self._expected)
            raw = self.path.read_bytes()
        except OSError as exc:
            return f"{type(exc).__name__}: {exc.strerror or 'error'}"
        try:
            doc = json.loads(raw)
        except ValueError:
            return "read-back is not JSON"
        if not has_relay(doc, self.relay):
            return "read-back lacks the PreToolUse/PermissionRequest relay"
        if raw != self._expected:
            return "read-back differs from the written baseline"
        return None
