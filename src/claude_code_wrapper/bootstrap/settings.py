"""Render `<CLAUDE_CONFIG_DIR>/settings.json`: relay hooks, permissions, overlay."""

import copy
import json
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any, NamedTuple

from claude_code_wrapper.config import Config
from claude_code_wrapper.hook_relay import CLAUDE_HOOK_EVENTS
from claude_code_wrapper.statusline import (
    STATUSLINE_ARGS,
    STATUSLINE_MODULE,
    STATUSLINE_SCRIPT,
    chain_path,
)

from .claude_json import _deep_merge, _load, atomic_write

PROMPT_HOOK_TIMEOUT = 15
CCW_MCP_ALLOW = "mcp__ccw__command"
PEERS_ALLOW = ("mcp__ccw__peers", "mcp__ccw__send_message")
PEERS_DENY = ("SendMessage", "ListAgents")
MIN_HOOK_TIMEOUT = 30
_GATED_EVENTS = (
    "onAgentToolCall",
    "onToolResult",
    "onTurnComplete",
    "onStop",
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


def _installed_script(script: str, module: str) -> str:
    """Absolute path of an installed console script, never repo-relative.

    Prefers the script beside the running interpreter (the venv the service was installed in),
    then PATH, then `python -m <module>` with this interpreter."""
    sibling = Path(sys.executable).parent / script
    if sibling.exists():
        return str(sibling)
    found = shutil.which(script)
    if found:
        return str(Path(found).absolute())
    return f"{shlex.quote(sys.executable)} -m {module}"


def default_relay_command() -> str:
    """Absolute path of the installed `ccw-hook`."""
    return _installed_script("ccw-hook", "claude_code_wrapper.hook_relay")


def default_statusline_command() -> str:
    """The statusLine command: the running `ccw` by absolute path, so it works off PATH."""
    resolved = _installed_script(STATUSLINE_SCRIPT, STATUSLINE_MODULE)
    if resolved.endswith(" -m " + STATUSLINE_MODULE):
        return resolved
    return f"{shlex.quote(resolved)} {STATUSLINE_ARGS}"


def _is_statusline_relay(command: Any) -> bool:
    """True when `command` is `<anything>/ccw statusline`, matched by basename."""
    if not isinstance(command, str):
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if len(parts) == 2 and parts[1] == STATUSLINE_ARGS:
        return Path(parts[0]).name == STATUSLINE_SCRIPT
    return len(parts) == 3 and parts[1:] == ["-m", STATUSLINE_MODULE]


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


def _install_statusline(doc: dict[str, Any], config_dir: str | Path) -> None:
    """Replace `doc["statusLine"]` with the relay; chain the command it held (after the overlay).

    A statusLine that already is the relay leaves the chain file alone, so a re-render never
    chains the relay to itself."""
    current = doc.get("statusLine")
    replacement: dict[str, Any] = {"type": "command", "command": default_statusline_command()}
    if isinstance(current, dict) and not _is_statusline_relay(current.get("command")):
        command = current.get("command")
        if isinstance(command, str) and command.strip():
            for key in ("padding", "refreshInterval"):
                if key in current:
                    replacement[key] = current[key]
            atomic_write(
                chain_path(config_dir), json.dumps({"command": command}, sort_keys=True) + "\n"
            )
    elif isinstance(current, dict):
        replacement.update(
            {k: v for k, v in current.items() if k in ("padding", "refreshInterval")}
        )
    doc["statusLine"] = replacement


def render_settings(
    config_dir: str | Path,
    cfg: Config,
    relay_command: str | None = None,
    ccw_mcp: bool = False,
    peers: bool = False,
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

    ccw_allow = (CCW_MCP_ALLOW, *PEERS_ALLOW)
    allow = [a for a in cfg.claude.permissions.allow if a not in ccw_allow]
    if ccw_mcp:
        allow.append(CCW_MCP_ALLOW)
    deny = list(cfg.claude.permissions.deny)
    if peers:
        allow.extend(PEERS_ALLOW)
        deny.extend(d for d in PEERS_DENY if d not in deny)
    if cfg.sessions.ask_user_question == "deny" and "AskUserQuestion" not in deny:
        deny.append("AskUserQuestion")
    permissions = doc.get("permissions") if isinstance(doc.get("permissions"), dict) else {}
    permissions.update(
        defaultMode=cfg.claude.permission_mode,
        allow=allow,
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

    _install_statusline(doc, config_dir)

    atomic_write(path, json.dumps(doc, indent=2, sort_keys=True) + "\n")
    return doc
