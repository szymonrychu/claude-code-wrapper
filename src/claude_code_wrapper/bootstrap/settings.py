"""Render `<CLAUDE_CONFIG_DIR>/settings.json`: relay hooks, permissions, overlay."""

import copy
import json
import shlex
import sys
from pathlib import Path
from typing import Any, NamedTuple

from claude_code_wrapper.config import Config
from claude_code_wrapper.hook_relay import CLAUDE_HOOK_EVENTS

from .claude_json import _deep_merge, _load, atomic_write

PROMPT_HOOK_TIMEOUT = 15
MIN_HOOK_TIMEOUT = 30
_GATED_EVENTS = (
    "onAgentToolCall",
    "onToolResult",
    "onTurnComplete",
    "onSubagentStart",
    "onSubagentStop",
)


class RelayTimeouts(NamedTuple):
    hook_timeout: float
    attempt_timeout: float


def relay_timeouts(cfg: Config) -> RelayTimeouts:
    """The one place the relay timeout formula lives."""
    blocking = [
        a.timeout_seconds
        for event in _GATED_EVENTS
        for a in cfg.hooks.for_event(event)
        if a.blocking
    ]
    hook_timeout = max(MIN_HOOK_TIMEOUT, max(blocking, default=0) + 5)
    return RelayTimeouts(hook_timeout, (hook_timeout - 2) / 2)


def default_relay_command() -> str:
    sibling = Path(sys.executable).parent / "ccw-hook"
    return str(sibling) if sibling.exists() else "ccw-hook"


def _is_relay(hook: Any, relay: str) -> bool:
    command = hook.get("command") if isinstance(hook, dict) else None
    if not isinstance(command, str):
        return False
    if command == relay or command.startswith(relay + " "):
        return True
    try:
        first = shlex.split(command)[0]
    except (ValueError, IndexError):
        return False
    return Path(first).name == "ccw-hook" or first == relay


def _without_relay(groups: Any, relay: str) -> list[Any]:
    kept = []
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict):
            kept.append(group)
            continue
        hooks = [h for h in group.get("hooks", []) if not _is_relay(h, relay)]
        if hooks or "hooks" not in group:
            kept.append({**group, "hooks": hooks} if "hooks" in group else group)
    return kept


def render_settings(
    config_dir: str | Path, cfg: Config, relay_command: str | None = None
) -> dict[str, Any]:
    path = Path(config_dir) / "settings.json"
    relay = relay_command or default_relay_command()
    doc = _load(path)
    timeouts = relay_timeouts(cfg)

    hooks = doc.get("hooks") if isinstance(doc.get("hooks"), dict) else {}
    for event in CLAUDE_HOOK_EVENTS:
        timeout = PROMPT_HOOK_TIMEOUT if event == "UserPromptSubmit" else timeouts.hook_timeout
        group = {
            "matcher": "",
            "hooks": [{"type": "command", "command": f"{relay} {event}", "timeout": timeout}],
        }
        hooks[event] = [group, *_without_relay(hooks.get(event), relay)]
    doc["hooks"] = hooks

    deny = list(cfg.claude.permissions.deny)
    if cfg.sessions.ask_user_question == "deny" and "AskUserQuestion" not in deny:
        deny.append("AskUserQuestion")
    permissions = doc.get("permissions") if isinstance(doc.get("permissions"), dict) else {}
    permissions.update(
        defaultMode=cfg.claude.permission_mode,
        allow=list(cfg.claude.permissions.allow),
        deny=deny,
    )
    doc["permissions"] = permissions
    doc["enableAllProjectMcpServers"] = True

    if cfg.plugins.disable:
        plugins = doc.get("enabledPlugins") if isinstance(doc.get("enabledPlugins"), dict) else {}
        for plugin_id in cfg.plugins.disable:
            plugins[plugin_id] = False
        doc["enabledPlugins"] = plugins

    overlay = copy.deepcopy(cfg.claude.settings)
    overlay_hooks = overlay.pop("hooks", None)
    _deep_merge(doc, overlay)
    if isinstance(overlay_hooks, dict):
        for event, groups in overlay_hooks.items():
            existing = [g for g in doc["hooks"].get(event, [])]
            for group in groups if isinstance(groups, list) else [groups]:
                if group not in existing:
                    existing.append(group)
            doc["hooks"][event] = existing

    atomic_write(path, json.dumps(doc, indent=2, sort_keys=True) + "\n")
    return doc
