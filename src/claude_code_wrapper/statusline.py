"""`ccw statusline`: relay Claude Code's statusLine JSON to the wrapper, then run the chain.

Stdlib only. The wrapper installs this as the session's `statusLine.command`; the user's own
statusLine command (if any) is kept in a chain file beside `settings.json` and run here with the
same stdin, its stdout printed. Posting is best effort with a short timeout and never fails the
statusline.

Environment: CCW_INTERNAL_URL, CCW_SESSION_ID, CCW_HOOK_TOKEN (as for the hook relay) and
CLAUDE_CONFIG_DIR (where the chain file lives).
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

from claude_code_wrapper.hook_relay import (
    ENV_SESSION,
    ENV_TOKEN,
    ENV_URL,
    HEADER_SESSION,
    HEADER_TOKEN,
    MAX_STDIN,
)

STATUSLINE_COMMAND = "ccw statusline"
STATUSLINE_PATH = "/internal/statusline"
CHAIN_FILE = "ccw-statusline-chain.json"
POST_TIMEOUT = 2.0
CHAIN_TIMEOUT = 30.0


def chain_path(config_dir: str | Path) -> Path:
    return Path(config_dir) / CHAIN_FILE


def post_statusline(body: bytes, timeout: float = POST_TIMEOUT) -> bool:
    """Best-effort POST of the statusLine JSON; True when the service accepted it."""
    base = os.environ.get(ENV_URL)
    session = os.environ.get(ENV_SESSION)
    if not base or not session:
        return False
    req = urllib.request.Request(
        base.rstrip("/") + STATUSLINE_PATH,
        data=body,
        headers={
            "Content-Type": "application/json",
            HEADER_SESSION: session,
            HEADER_TOKEN: os.environ.get(ENV_TOKEN, ""),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return 200 <= resp.status < 300
    except Exception:
        return False


def chained_command() -> str | None:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if not config_dir:
        return None
    try:
        data: Any = json.loads(chain_path(config_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    command = data.get("command") if isinstance(data, dict) else None
    return command if isinstance(command, str) and command.strip() else None


def run_chain(command: str, body: bytes) -> str:
    try:
        done = subprocess.run(  # noqa: S602
            command,
            input=body,
            shell=True,
            capture_output=True,
            timeout=CHAIN_TIMEOUT,
            check=False,
        )
    except Exception:
        return ""
    return done.stdout.decode("utf-8", errors="replace")


def main() -> int:
    body = sys.stdin.buffer.read(MAX_STDIN)
    with contextlib.suppress(Exception):
        post_statusline(body)
    command = chained_command()
    if command:
        sys.stdout.write(run_chain(command, body))
        sys.stdout.flush()
    return 0
