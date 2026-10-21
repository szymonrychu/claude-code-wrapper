#!/usr/bin/env python3
"""Tatara's turn-path harness behaviour as ccw hooks (stdlib only, one script, subcommands).

  finalize       onTurnComplete (non-blocking): commit and push repos, post the signed callback
  capture-issue  onAgentToolCall (blocking, matcher mcp__tatara__report_internal_issue)
  outcome-result onToolResult (non-blocking, matcher mcp__tatara__submit_outcome)
  outcome-gate   onStop (blocking): re-prompt while the last submit_outcome was rejected
  statusline     claude.settings.statusLine command (runs inside claude)

Hook context comes from the env ccw sets: CCW_STATE_DIR, CCW_CLAUDE_CONFIG_DIR, CCW_WORKDIR.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

CATEGORIES = {
    "tool_failure",
    "missing_context",
    "unclear_task",
    "permission",
    "environment",
    "other",
}
SEVERITIES = {"low", "medium", "high", "critical"}
STATES = {"completed": "complete", "failed": "failed", "interrupted": "failed", "timeout": "failed"}
CLAUDE_STOP_CAP = 8  # claude ends the turn after this many consecutive Stop blocks
DEFAULT_REPROMPTS = 2
REPROMPT_TEXT = (
    "Your submit_outcome call was rejected. "
    "Fix the problem it reported and call submit_outcome again."
)
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__"}


def state_dir() -> Path:
    path = Path(os.environ.get("CCW_STATE_DIR") or ".")
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def envelope() -> dict[str, Any]:
    try:
        doc = json.load(sys.stdin)
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


# ---------------------------------------------------------------- issue capture


def capture_issue() -> int:
    tool_input = (envelope().get("data") or {}).get("tool_input") or {}
    category = tool_input.get("category")
    severity = tool_input.get("severity")
    issue = {
        "category": category if category in CATEGORIES else "other",
        "severity": severity if severity in SEVERITIES else "medium",
        "summary": str(tool_input.get("summary") or ""),
    }
    with (state_dir() / "issues.jsonl").open("a") as fh:
        fh.write(json.dumps(issue) + "\n")
    print(json.dumps({"decision": "allow"}))
    return 0


def drain_issues() -> list[dict[str, Any]]:
    path = state_dir() / "issues.jsonl"
    draining = path.with_name("issues.draining")
    try:
        os.replace(path, draining)
    except OSError:
        return []
    issues = []
    for line in draining.read_text().splitlines():
        try:
            issues.append(json.loads(line))
        except ValueError:
            continue
    draining.unlink(missing_ok=True)
    return issues


# ---------------------------------------------------------------- outcome gate


def outcome_result() -> int:
    env = envelope()
    data = env.get("data") or {}
    record = {"turn_id": env.get("turn_id"), "is_error": bool(data.get("is_error"))}
    write_atomic(state_dir() / "outcome.json", json.dumps(record))
    return 0


def reprompt_cap() -> int:
    try:
        wanted = int(os.environ.get("MAX_OUTCOME_REPROMPTS") or DEFAULT_REPROMPTS)
    except ValueError:
        wanted = DEFAULT_REPROMPTS
    return max(0, min(wanted, CLAUDE_STOP_CAP - 1))


def reprompt_count(turn_id: str) -> int:
    try:
        return int((state_dir() / f"reprompts-{turn_id}").read_text().strip())
    except (OSError, ValueError):
        return 0


def reprompt_pending(turn_id: str) -> bool:
    outcome = read_json(state_dir() / "outcome.json")
    if not isinstance(outcome, dict):
        return False
    rejected = outcome.get("turn_id") == turn_id and outcome.get("is_error") is True
    return rejected and reprompt_count(turn_id) < reprompt_cap()


def outcome_gate() -> int:
    turn_id = str(envelope().get("turn_id") or "")
    if not turn_id or not reprompt_pending(turn_id):
        return 0
    write_atomic(state_dir() / f"reprompts-{turn_id}", str(reprompt_count(turn_id) + 1))
    reason = os.environ.get("OUTCOME_REPROMPT_TEXT") or REPROMPT_TEXT
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


# ---------------------------------------------------------------- statusline


def config_dir() -> Path:
    return Path(
        os.environ.get("CLAUDE_CONFIG_DIR")
        or os.environ.get("CCW_CLAUDE_CONFIG_DIR")
        or Path.home() / ".claude"
    )


def statusline() -> int:
    doc = envelope()
    limits = doc.get("rate_limits")
    if limits:
        directory = config_dir()
        directory.mkdir(parents=True, exist_ok=True)
        write_atomic(directory / "rate_limits.json", json.dumps(limits))
    model = (doc.get("model") or {}).get("display_name") or "claude"
    cwd = Path((doc.get("workspace") or {}).get("current_dir") or doc.get("cwd") or ".").name
    print(f"{model} | {cwd or '.'}")
    return 0


# ---------------------------------------------------------------- finaliser


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=120, check=False
    )


def find_repos(root: Path) -> list[Path]:
    found = []
    for current, dirs, _ in os.walk(root):
        if ".git" in dirs or (Path(current) / ".git").exists():
            found.append(Path(current))
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    return sorted(found)


def push_repos(workdir: Path, task: str) -> tuple[list[str], list[dict[str, str]]]:
    pushed: list[str] = []
    failed: list[dict[str, str]] = []
    name = os.environ.get("GIT_USER_NAME") or "tatara"
    email = os.environ.get("GIT_USER_EMAIL") or "tatara@localhost"
    for repo in find_repos(workdir):
        label = str(repo.relative_to(workdir)) or "."
        status = git(repo, "status", "--porcelain")
        if status.returncode != 0:
            failed.append({"repo": label, "error": status.stderr.strip()})
            continue
        if not status.stdout.strip():
            continue
        steps = [
            ("add", "-A"),
            (
                "-c",
                f"user.name={name}",
                "-c",
                f"user.email={email}",
                "commit",
                "-m",
                f"tatara: {task}",
            ),
            ("push", "origin", "HEAD"),
        ]
        for step in steps:
            result = git(repo, *step)
            if result.returncode != 0:
                failed.append({"repo": label, "error": (result.stderr or result.stdout).strip()})
                break
        else:
            pushed.append(label)
    return pushed, failed


def duration(record: dict[str, Any]) -> float:
    try:
        start = datetime.fromisoformat(record["started_at"])
        end = datetime.fromisoformat(record["completed_at"])
        return round((end - start).total_seconds(), 3)
    except (KeyError, TypeError, ValueError):
        return 0.0


def finalize() -> int:
    env = envelope()
    record = env.get("data") or {}
    turn_id = str(record.get("turn_id") or env.get("turn_id") or "")
    if turn_id and reprompt_pending(turn_id) and record.get("state") == "completed":
        return 0  # an outcome re-prompt is pending: the turn is not final yet
    task = os.environ.get("TATARA_TASK") or ""
    workdir = Path(env.get("workdir") or os.environ.get("CCW_WORKDIR") or ".")
    pushed, failed = push_repos(workdir, task)
    payload: dict[str, Any] = {
        "turnId": turn_id,
        "taskName": task,
        "state": STATES.get(str(record.get("state")), "failed"),
        "finalText": record.get("final_text"),
        "stopReason": record.get("stop_reason"),
        "error": record.get("error"),
        "durationSeconds": duration(record),
        "usage": record.get("usage") or {},
        "pushedRepos": pushed,
        "failedRepos": failed,
        "internalIssues": drain_issues(),
    }
    limits = read_json(config_dir_for_hooks() / "rate_limits.json")
    if limits is not None:
        payload["accountUsage"] = limits
    return post(payload)


def config_dir_for_hooks() -> Path:
    return Path(os.environ.get("CCW_CLAUDE_CONFIG_DIR") or config_dir())


def post(payload: dict[str, Any]) -> int:
    url = os.environ.get("DEFAULT_CALLBACK_URL")
    if not url:
        print("DEFAULT_CALLBACK_URL is not set", file=sys.stderr)
        return 1
    body = json.dumps(payload).encode()
    secret = os.environ.get("CALLBACK_HMAC_SECRET", "")
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "X-Tatara-Signature": f"sha256={signature}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30):
            return 0
    except OSError as e:
        print(f"callback failed: {e}", file=sys.stderr)
        return 1


COMMANDS = {
    "finalize": finalize,
    "capture-issue": capture_issue,
    "outcome-result": outcome_result,
    "outcome-gate": outcome_gate,
    "statusline": statusline,
}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in COMMANDS:
        print(f"usage: tatara_hooks.py {{{'|'.join(COMMANDS)}}}", file=sys.stderr)
        return 64
    return COMMANDS[argv[1]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
