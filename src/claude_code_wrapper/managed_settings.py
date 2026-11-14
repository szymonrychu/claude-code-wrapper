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
from claude_code_wrapper.claude_env import CREDENTIAL_ENV, SECRET_KEY_RE

log = logging.getLogger(__name__)

ManagedState = Literal["active", "fallback", "degraded"]
TMP_NAME = ".managed-settings.json.ccw-tmp"
#: Names Claude Code itself reads beside the managed file; allowed in the managed dir (R1).
CLAUDE_MANAGED_NAMES = frozenset({"managed-settings.d", "managed-mcp.json"})


#: Keys the overlay may never change: the baseline's own values (dotted path -> value).
LOCKED: dict[str, Any] = {
    "disableAgentView": True,
    "pluginConfigs.cc-plugin-sec-default@builtin.options.allowManagedModsOnly": True,
    "env.CLAUDE_CODE_EXPERIMENTAL_CODE_MODE": "0",
    "env.CLAUDE_CODE_DISABLE_AGENT_VIEW": "1",
}
#: Kill switches: the overlay may state them false (the baseline default), never truthy (R8).
FORBIDDEN_TRUE = ("disableAllHooks", "allowManagedHooksOnly")
_MISSING = object()


class ManagedSettingsError(ValueError):
    """The overlay is refused."""


class LockedKeyError(ManagedSettingsError):
    def __init__(self, path: str) -> None:
        super().__init__(f"overlay may not change the locked key {path}")
        self.path = path


class OverlaySecretError(ManagedSettingsError):
    def __init__(self) -> None:
        super().__init__("overlay env may not carry credentials; use claude.secret_env")


class ManagedSettingsUnavailable(RuntimeError):
    """The managed file is not active (fallback or degraded): no overlay can be applied."""


def _lookup(doc: dict[str, Any], path: str) -> Any:
    """The value at dotted `path`; _MISSING when absent. A non-dict where the path continues
    would replace the baseline's dict, so it counts as a change: raises LockedKeyError."""
    node: Any = doc
    parts = path.split(".")
    for part in parts:
        if not isinstance(node, dict):
            raise LockedKeyError(path)
        if part not in node:
            return _MISSING
        node = node[part]
    return node


def _deep(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = base
    for key, val in over.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def _list_of(value: Any, what: str) -> list[Any]:
    if not isinstance(value, list):
        raise ManagedSettingsError(f"overlay {what} must be a list")
    return value


def merge_overlay(baseline: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """The baseline with `overlay` applied (spec D2, R7). Dicts deep merge; each hooks event
    list is the baseline groups then the overlay groups; `permissions.deny` is the baseline
    rules then the overlay rules not already present; any other list is replaced. Refuses a
    LOCKED key set to another value, a FORBIDDEN_TRUE key set truthy, and credential env."""
    for path, locked in LOCKED.items():
        found = _lookup(overlay, path)
        if found is not _MISSING and found != locked:
            raise LockedKeyError(path)
    for key in FORBIDDEN_TRUE:
        if overlay.get(key):
            raise LockedKeyError(key)
    env = overlay.get("env")
    if isinstance(env, dict) and any(
        SECRET_KEY_RE.search(str(name)) or name in CREDENTIAL_ENV for name in env
    ):
        raise OverlaySecretError
    out = copy.deepcopy(baseline)
    hooks = overlay.get("hooks")
    perms = overlay.get("permissions")
    if "hooks" in overlay and not isinstance(hooks, dict):
        raise ManagedSettingsError("overlay hooks must be an object")
    if "permissions" in overlay and not isinstance(perms, dict):
        raise ManagedSettingsError("overlay permissions must be an object")
    deny: list[Any] | None = None
    if isinstance(perms, dict) and "deny" in perms:
        deny = _list_of(perms["deny"], "permissions.deny")
    rest = {k: v for k, v in overlay.items() if k != "hooks"}
    if deny is not None:
        rest["permissions"] = {k: v for k, v in perms.items() if k != "deny"}
    _deep(out, rest)
    if hooks:
        merged = out.setdefault("hooks", {})
        for event, groups in hooks.items():
            merged[event] = [
                *_list_of(merged.get(event, []), f"hooks.{event}"),
                *copy.deepcopy(_list_of(groups, f"hooks.{event}")),
            ]
    if deny:
        rules = out.setdefault("permissions", {}).setdefault("deny", [])
        rules.extend(r for r in deny if r not in rules)
    return out


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


@dataclass
class ManagedSnapshot:
    status: ManagedStatus
    document: dict[str, Any]


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
        self._current: dict[str, Any] = copy.deepcopy(baseline)
        self._expected = serialize(self._current)
        self._sha256 = hashlib.sha256(self._expected).hexdigest()

    @property
    def document(self) -> dict[str, Any]:
        """What the file should hold: the baseline with the overlay applied."""
        return copy.deepcopy(self._current)

    def snapshot(self) -> ManagedSnapshot:
        """Status and document read together under the lock, so a concurrent replace cannot
        tear them apart."""
        with self._lock:
            return ManagedSnapshot(self.status(), self.document)

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

    def refresh(self, trigger: str = "api") -> ManagedStatus:
        """The status, after healing a degraded file first (R3)."""
        with self._lock:
            self._heal(trigger)
            return self.status()

    def replace(self, overlay: dict[str, Any]) -> ManagedStatus:
        """Make `overlay` (not a patch) the overlay: merge, write, then update what the file
        must hold, all under the lock. On OSError the old file, bytes and overlay stay."""
        with self._lock:
            self._heal("api")
            if self._state != "active":
                raise ManagedSettingsUnavailable(f"managed settings are {self._state}")
            merged = merge_overlay(self._baseline, overlay)
            if not has_relay(merged, self.relay):
                raise ManagedSettingsError("overlay would remove the relay hooks")
            data = serialize(merged)
            write_managed(self.path, data)
            self._current, self._expected = merged, data
            self._sha256 = hashlib.sha256(data).hexdigest()
            self._overlay = copy.deepcopy(overlay)
            return self.status()

    def _heal(self, trigger: str) -> None:
        """Degraded -> rewrite the expected bytes; active again only when read back equal."""
        if self._state != "degraded":
            return
        try:
            write_managed(self.path, self._expected)
            ok = self.path.read_bytes() == self._expected
        except OSError:
            ok = False
        if ok:
            self._state, self._reason = "active", None
            self._restores += 1
            log.warning("managed settings %s healed (%s)", self.path, trigger)

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
