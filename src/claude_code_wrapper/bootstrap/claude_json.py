"""Seed `<CLAUDE_CONFIG_DIR>/.claude.json` so sessions boot without dialogs."""

import contextlib
import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from claude_code_wrapper.config import ClaudeConfig

from .errors import BootstrapError


def ensure_config_dir(config_dir: str | Path) -> Path:
    path = Path(config_dir)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def atomic_write(path: Path, data: str, mode: int = 0o600) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BootstrapError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BootstrapError(f"{path} is not a JSON object")
    return data


#: Env references claude expands itself, so the hook token is never written to a file.
CCW_MCP_ENTRY: dict[str, Any] = {
    "type": "http",
    "url": "${CCW_INTERNAL_URL}/internal/mcp",
    "headers": {
        "X-CCW-Session": "${CCW_SESSION_ID}",
        "X-CCW-Hook-Token": "${CCW_HOOK_TOKEN}",
    },
    "alwaysLoad": True,
}


def seed_claude_json(
    config_dir: str | Path,
    workdir: str | Path,
    claude_cfg: ClaudeConfig,
    env: Any,
    ccw_mcp: bool = False,
) -> Path:
    config_dir = ensure_config_dir(config_dir)
    target = config_dir / ".claude.json"
    data = _load(target)

    data["hasCompletedOnboarding"] = True
    data["autoUpdates"] = False
    if claude_cfg.permission_mode == "bypassPermissions":
        data["bypassPermissionsModeAccepted"] = True

    projects = data.setdefault("projects", {})
    absolute = os.path.abspath(workdir)
    for key in dict.fromkeys((os.path.realpath(workdir), absolute)):
        entry = projects.setdefault(key, {})
        entry["hasTrustDialogAccepted"] = True

    api_key = getattr(env, "anthropic_api_key", None)
    if api_key:
        responses = data.setdefault("customApiKeyResponses", {})
        approved = responses.setdefault("approved", [])
        responses.setdefault("rejected", [])
        raw = api_key.get_secret_value() if hasattr(api_key, "get_secret_value") else str(api_key)
        suffix = raw[-20:]
        if suffix not in approved:
            approved.append(suffix)

    if claude_cfg.mcp_servers:
        servers = data.setdefault("mcpServers", {})
        for name, server in claude_cfg.mcp_servers.items():
            if server.type == "stdio":
                servers[name] = {
                    "type": "stdio",
                    "command": server.command,
                    "args": list(server.args),
                    "env": dict(server.env),
                }
            else:
                servers[name] = {
                    "type": server.type,
                    "url": server.url,
                    "headers": dict(server.headers),
                }

    _deep_merge(data, claude_cfg.claude_json)
    if ccw_mcp:
        data.setdefault("mcpServers", {})["ccw"] = copy.deepcopy(CCW_MCP_ENTRY)
    elif (
        isinstance(data.get("mcpServers"), dict) and data["mcpServers"].get("ccw") == CCW_MCP_ENTRY
    ):
        del data["mcpServers"]["ccw"]
    atomic_write(target, json.dumps(data, indent=2))
    return target
