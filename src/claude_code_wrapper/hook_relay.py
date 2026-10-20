"""ccw-hook: relay a Claude Code hook call to the wrapper service (protocol D7).

Stdlib only so it starts fast inside every hook call. Never raises and never
blocks claude beyond its bounded retries: every failure ends in exit 0.

Environment:
    CCW_INTERNAL_URL, CCW_SESSION_ID, CCW_HOOK_TOKEN  service address and identity
    CCW_TOOL_GATE_FAIL=deny   PreToolUse prints a deny when the service is unreachable
    CCW_GATE_TIMEOUT          per-attempt timeout seconds for all but UserPromptSubmit (14)
    CCW_RELAY_BACKOFF         initial retry backoff seconds (default 0.5, doubles);
                              tests set it small to stay fast
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import uuid
from typing import TypedDict

CLAUDE_HOOK_EVENTS = (
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
    "SubagentStart",
    "SubagentStop",
)
RELAY_PATH = "/internal/hooks/{event}"
HEADER_SESSION = "X-CCW-Session"
HEADER_TOKEN = "X-CCW-Hook-Token"
HEADER_DELIVERY = "X-CCW-Delivery"
ENV_URL = "CCW_INTERNAL_URL"
ENV_SESSION = "CCW_SESSION_ID"
ENV_TOKEN = "CCW_HOOK_TOKEN"
ENV_GATE_FAIL = "CCW_TOOL_GATE_FAIL"
ENV_GATE_TIMEOUT = "CCW_GATE_TIMEOUT"
ENV_BACKOFF = "CCW_RELAY_BACKOFF"

MAX_STDIN = 8 * 1024 * 1024
DEFAULT_GATE_TIMEOUT = 14.0
DEFAULT_BACKOFF = 0.5


class RelayResponse(TypedDict):
    stdout: str
    stderr: str
    exit_code: int


def deny_output(reason: str) -> str:
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, ""))
    except ValueError:
        return default


def _post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> RelayResponse | None:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            if resp.status != 200:
                return None
            data = json.loads(resp.read())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return RelayResponse(
            stdout=str(data.get("stdout", "")),
            stderr=str(data.get("stderr", "")),
            exit_code=int(data.get("exit_code", 0)),
        )
    except (TypeError, ValueError):
        return None


def _relay(event: str) -> int:
    base = os.environ.get(ENV_URL)
    session = os.environ.get(ENV_SESSION)
    if not base or not session:
        return 0
    body = sys.stdin.buffer.read(MAX_STDIN)
    headers = {
        "Content-Type": "application/json",
        HEADER_SESSION: session,
        HEADER_TOKEN: os.environ.get(ENV_TOKEN, ""),
        HEADER_DELIVERY: str(uuid.uuid4()),
    }
    url = base.rstrip("/") + RELAY_PATH.format(event=event)
    if event == "UserPromptSubmit":
        attempts, timeout = 3, 5.0
    else:
        attempts, timeout = 2, _float_env(ENV_GATE_TIMEOUT, DEFAULT_GATE_TIMEOUT)
    delay = _float_env(ENV_BACKOFF, DEFAULT_BACKOFF)
    for i in range(attempts):
        if i:
            time.sleep(delay)
            delay *= 2
        result = _post(url, body, headers, timeout)
        if result is not None:
            sys.stdout.write(result["stdout"])
            sys.stderr.write(result["stderr"])
            sys.stdout.flush()
            sys.stderr.flush()
            return result["exit_code"]
    return _failure(event)


def _failure(event: str) -> int:
    if event == "PreToolUse" and os.environ.get(ENV_GATE_FAIL) == "deny":
        sys.stdout.write(deny_output("claude-code-wrapper is unreachable"))
        sys.stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv if argv is None else argv
    event = args[1] if len(args) > 1 else ""
    if event not in CLAUDE_HOOK_EVENTS:
        return 0
    try:
        return _relay(event)
    except Exception:
        try:
            return _failure(event)
        except Exception:
            return 0


if __name__ == "__main__":
    sys.exit(main())
