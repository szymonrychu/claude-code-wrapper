#!/usr/bin/env python3
"""Tatara's turn-path harness behaviour as ccw hooks (stdlib only, one script, subcommands).

  finalize       onTurnComplete (non-blocking): commit and push repos, post the signed callback
  capture-issue  onAgentToolCall (blocking, matcher mcp__tatara__report_internal_issue)
  outcome-result onToolResult (non-blocking, matcher mcp__tatara__submit_outcome)
  outcome-gate   onStop (blocking): re-prompt while the last submit_outcome was rejected
  statusline     claude.settings.statusLine command (runs inside claude)
  ttl-gate       onTurn (blocking): deny turns after AGENT_POD_TTL_SECONDS, admit one handoff turn
  safety-push    onTick (when busy): git push the task branch of every repo, never commit
  metrics-push   onTick (when always): forward ccw's /metrics to OPERATOR_PUSH_URL
  bootstrap      init.setup_commands: skeleton of tatara's bootstrap (clone, branch, CLAUDE.md, MCP)

Hook context comes from the env ccw sets: CCW_STATE_DIR, CCW_CLAUDE_CONFIG_DIR, CCW_WORKDIR.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
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


# ---------------------------------------------------------------- ttl gate


def ttl_gate() -> int:
    """First turn records t0. Past the TTL ordinary turns are denied and one handoff turn is
    admitted (metadata.handoff true); the slot is only consumed after the TTL."""
    try:
        ttl = float(os.environ.get("AGENT_POD_TTL_SECONDS") or 0)
    except ValueError:
        ttl = 0.0
    metadata = (envelope().get("data") or {}).get("metadata") or {}
    path = state_dir() / "ttl.json"
    doc = read_json(path)
    if not isinstance(doc, dict) or "t0" not in doc:
        doc = {"t0": time.time(), "handoff_used": False}
    expired = ttl > 0 and time.time() - float(doc["t0"]) >= ttl
    verdict: dict[str, Any] = {"decision": "allow"}
    if expired:
        if metadata.get("handoff") is True and not doc.get("handoff_used"):
            doc["handoff_used"] = True
        else:
            verdict = {"decision": "deny", "reason": "pod ttl exceeded"}
    write_atomic(path, json.dumps(doc))
    print(json.dumps(verdict))
    return 0


# ---------------------------------------------------------------- safety push


def hook_workdir() -> Path:
    return Path(os.environ.get("CCW_WORKDIR") or ".")


def safety_push() -> int:
    """Push only. Commits belong to the finaliser, so uncommitted work is never touched."""
    branch = os.environ.get("TASK_BRANCH") or "HEAD"
    workdir = hook_workdir()
    failed = 0
    for repo in find_repos(workdir):
        result = git(repo, "push", "origin", branch)
        if result.returncode != 0:
            failed += 1
            print(f"{repo.relative_to(workdir)}: {result.stderr.strip()}", file=sys.stderr)
    return 1 if failed else 0


# ---------------------------------------------------------------- metrics push


def metrics_push() -> int:
    target = os.environ.get("OPERATOR_PUSH_URL")
    if not target:
        return 0
    api = (os.environ.get("CCW_API_URL") or "").rstrip("/")
    headers = {"Content-Type": "text/plain; version=0.0.4"}
    for name in ("RUN_ID", "POD_NAME"):
        headers[name] = os.environ.get(name, "")
    try:
        with urllib.request.urlopen(f"{api}/metrics", timeout=10) as resp:
            text = resp.read()
        request = urllib.request.Request(target, data=text, headers=headers)
        with urllib.request.urlopen(request, timeout=20):
            return 0
    except (OSError, ValueError) as e:
        print(f"metrics push failed: {e}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------- bootstrap

BLOCK_START = "<!-- tatara:{name} -->"
BLOCK_END = "<!-- /tatara:{name} -->"
DIRECTIVES = {
    "headless": (
        "You run headless: nobody answers questions. Decide on your own, "
        "and report blockers through the tatara tools instead of asking."
    ),
    "auto-push": (
        "Commit your work on the task branch and push it to origin as you go; "
        "unpushed work is lost when the pod is replaced."
    ),
}


def drop_block(text: str, name: str) -> str:
    start, end = BLOCK_START.format(name=name), BLOCK_END.format(name=name)
    head, found, rest = text.partition(start)
    if not found:
        return text
    return head + rest.partition(end)[2].lstrip("\n")


def write_directives(config_dir: Path) -> None:
    """Add the directive blocks to CLAUDE.md, replacing earlier copies so reruns do not stack."""
    config_dir.mkdir(parents=True, exist_ok=True)
    target = config_dir / "CLAUDE.md"
    text = target.read_text() if target.exists() else ""
    for name, body in DIRECTIVES.items():
        text = drop_block(text, name).rstrip("\n")
        block = f"{BLOCK_START.format(name=name)}\n{body}\n{BLOCK_END.format(name=name)}\n"
        text = (text + "\n\n" if text else "") + block
    write_atomic(target, text)


def repo_specs() -> list[tuple[str, str]]:
    raw = os.environ.get("TATARA_REPOS")
    if raw:
        items = json.loads(raw)
        if not isinstance(items, list):
            raise ValueError("TATARA_REPOS must be a JSON list of {url, branch}")
        return [(str(i["url"]), str(i.get("branch") or "main")) for i in items]
    url = os.environ.get("REPO_URL")
    return [(url, os.environ.get("REPO_BRANCH") or "main")] if url else []


def owner_repo(url: str) -> Path:
    parts = [p for p in url.replace(":", "/").rstrip("/").split("/") if p]
    name = parts[-1].removesuffix(".git")
    return Path(parts[-2] if len(parts) > 1 else "_") / name


def clone_env() -> dict[str, str]:
    env = dict(os.environ)
    token = env.get("GIT_TOKEN")
    if token:  # config via env keeps the token out of argv
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="http.extraHeader",
            GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}",
        )
    return env


def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=clone_env(), capture_output=True, text=True, check=False
    )


def must_git(args: list[str], cwd: Path, what: str) -> str:
    result = run_git(args, cwd)
    if result.returncode != 0:
        raise RuntimeError(f"{what} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout


def prepare_repo(workdir: Path, url: str, base: str, task_branch: str, full: bool) -> None:
    dest = workdir / owner_repo(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not (dest / ".git").exists():
        depth = [] if full else ["--depth", "1"]
        must_git(["clone", *depth, "--branch", base, url, str(dest)], workdir, f"clone {url}")
    if not task_branch:
        return
    listed = run_git(["ls-remote", "--exit-code", "--heads", "origin", task_branch], dest)
    if listed.returncode == 0:
        depth = [] if full else ["--depth", "1"]
        refspec = f"+refs/heads/{task_branch}:refs/remotes/origin/{task_branch}"
        must_git(["fetch", *depth, "origin", refspec], dest, f"fetch {task_branch}")
        must_git(["checkout", "-B", task_branch, f"origin/{task_branch}"], dest, "checkout")
    else:
        must_git(["checkout", "-B", task_branch], dest, f"create {task_branch}")


def bootstrap() -> int:
    """A skeleton of tatara's bootstrap, not a port: it clones the repos, puts the task branch
    in place, writes the CLAUDE.md directive blocks and the MCP config. Tatara's Go bootstrap
    does more (credentials helpers, sparse checkouts, retries) and stays the reference."""
    workdir = Path.cwd()
    try:
        specs = repo_specs()
        mcp = os.environ.get("TATARA_EXTRA_MCP_SERVERS")
        servers = json.loads(mcp) if mcp else None
        if servers is not None and not isinstance(servers, dict):
            raise ValueError("TATARA_EXTRA_MCP_SERVERS must be a JSON object")
        full = (os.environ.get("TATARA_WORKSPACE_FULL_CLONE") or "").lower() in {"1", "true", "yes"}
        task_branch = os.environ.get("TASK_BRANCH") or ""
        for url, base in specs:
            prepare_repo(workdir, url, base, task_branch, full)
        config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        if config_dir:
            write_directives(Path(config_dir))
        if servers:
            write_atomic(workdir / ".mcp.json", json.dumps({"mcpServers": servers}, indent=2))
    except (RuntimeError, ValueError, KeyError, TypeError, OSError) as e:
        print(f"bootstrap: {e}", file=sys.stderr)
        return 1
    return 0


COMMANDS = {
    "finalize": finalize,
    "capture-issue": capture_issue,
    "outcome-result": outcome_result,
    "outcome-gate": outcome_gate,
    "statusline": statusline,
    "ttl-gate": ttl_gate,
    "safety-push": safety_push,
    "metrics-push": metrics_push,
    "bootstrap": bootstrap,
}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in COMMANDS:
        print(f"usage: tatara_hooks.py {{{'|'.join(COMMANDS)}}}", file=sys.stderr)
        return 64
    return COMMANDS[argv[1]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
