#!/usr/bin/env python3
"""magic-trainer's harness behaviour as ccw hooks (stdlib only, one script, subcommands).

  pending-tasks   onStop (blocking): defer while background work started this turn is unfinished
  review          onDestroy (blocking): run `claude -p` over data.input, return {"output": {...}}
  narrate         onAgentToolCall / onAgentResponse (non-blocking): one line per event to a log
  git-credential  git credential helper: serves the token from a file

Hook context comes from the env ccw sets (CCW_WORKDIR, CCW_CLAUDE_CONFIG_DIR, ...).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

TURN_MARKER = re.compile(r"<!--\s*ccw turn:\s*\S+?\s*-->")
TASK_ID = re.compile(r"<task-id>\s*([^<\s]+)\s*</task-id>")
STATUS = re.compile(r"<status>\s*([^<\s]+)\s*</status>")
STARTED_ID = re.compile(r"(?:\bID:|\bagentId:|\btask_id:|\bshell_id:)\s*([A-Za-z0-9_.:-]+)")
SHUTDOWN_FROM = re.compile(r"shutdown_approved[^<]*?\"?from\"?\s*[:=]\s*\"?([\w.-]+)")
SPAWNERS = {"Agent", "Task"}
STOPPERS = {"TaskStop", "KillShell", "KillBash"}
DONE_STATUSES = {"completed", "failed", "killed", "stopped", "cancelled", "error"}
NARRATION_DEFAULT = "/proc/1/fd/1"
SAY_CHARS = 80


def envelope() -> dict[str, Any]:
    try:
        doc = json.load(sys.stdin)
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def emit(doc: Any) -> None:
    sys.stdout.write(json.dumps(doc) + "\n")


# ---------------------------------------------------------------- pending-tasks


def _blocks(line: dict[str, Any]) -> list[dict[str, Any]]:
    content = (line.get("message") or {}).get("content")
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _text_of(line: dict[str, Any]) -> str:
    content = (line.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in _blocks(line):
        for key in ("text", "content"):
            value = block.get(key)
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, list):
                parts.extend(p.get("text", "") for p in value if isinstance(p, dict))
    return "\n".join(parts)


def _is_prompt(line: dict[str, Any]) -> bool:
    """A user line that carries the turn marker (the prompt ccw pasted)."""
    return line.get("type") == "user" and bool(TURN_MARKER.search(_text_of(line)))


def read_turn_lines(path: str) -> list[dict[str, Any]]:
    """Transcript lines since the turn's last marker (the whole file when there is none)."""
    lines: list[dict[str, Any]] = []
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for text in raw:
        try:
            doc = json.loads(text)
        except ValueError:
            continue
        if not isinstance(doc, dict):
            continue
        if _is_prompt(doc):
            lines = []
        lines.append(doc)
    return lines


def _spawned(name: str, tinput: dict[str, Any]) -> str | None:
    """`teammate`, `agent` or `bash` when this tool use starts background work."""
    if name in SPAWNERS:
        if tinput.get("team_name"):
            return "teammate"
        if tinput.get("run_in_background"):
            return "agent"
    elif name == "Bash" and tinput.get("run_in_background"):
        return "bash"
    return None


def pending_tasks(lines: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Background work started in `lines` and neither finished nor stopped."""
    started: dict[str, dict[str, Any]] = {}  # tool_use_id -> task
    for line in lines:
        for block in _blocks(line):
            if block.get("type") == "tool_use":
                tinput = block.get("input") if isinstance(block.get("input"), dict) else {}
                kind = _spawned(str(block.get("name")), tinput)
                if kind:
                    name = str(tinput.get("name") or tinput.get("description") or "")
                    started[str(block.get("id"))] = {
                        "kind": kind,
                        "name": name,
                        "id": name if kind == "teammate" and name else str(block.get("id")),
                        "ids": {str(block.get("id"))}
                        | ({name} if kind == "teammate" and name else set()),
                    }
    finished: set[str] = set()
    for line in lines:
        text = _text_of(line)
        for block in _blocks(line):
            if block.get("type") == "tool_result":
                task = started.get(str(block.get("tool_use_id")))
                result = _text_of({"message": {"content": [block]}})
                found = STARTED_ID.search(result)
                if task and found:
                    task["ids"].add(found.group(1))
                    if task["kind"] != "teammate":
                        task["id"] = found.group(1)
            elif block.get("type") == "tool_use" and block.get("name") in STOPPERS:
                tinput = block.get("input") or {}
                for key in ("task_id", "shell_id", "bash_id", "id"):
                    if tinput.get(key):
                        finished.add(str(tinput[key]))
        for match in TASK_ID.finditer(text):
            status = STATUS.search(text[match.end() :]) or STATUS.search(text)
            if status is None or status.group(1).lower() in DONE_STATUSES:
                finished.add(match.group(1))
        for match in SHUTDOWN_FROM.finditer(text):
            finished.add(match.group(1))
    return [
        {"kind": t["kind"], "name": t["name"], "id": t["id"]}
        for t in started.values()
        if not t["ids"] & finished
    ]


def cmd_pending_tasks() -> int:
    data = envelope().get("data") or {}
    path = data.get("transcript_path")
    pending = pending_tasks(read_turn_lines(path)) if isinstance(path, str) and path else []
    emit({"decision": "defer" if pending else "allow", "annotations": {"pending_tasks": pending}})
    return 0


# ---------------------------------------------------------------- review


def cmd_review() -> int:
    data = envelope().get("data") or {}
    spec = data.get("input")
    if not isinstance(spec, dict) or not spec.get("prompt"):
        emit({"output": None})
        return 0
    argv = [
        os.environ.get("CLAUDE_BIN") or "claude",
        "-p",
        "--setting-sources",
        "",
        "--strict-mcp-config",
    ]
    if spec.get("model"):
        argv += ["--model", str(spec["model"])]
    env = dict(os.environ)
    if os.environ.get("CCW_CLAUDE_CONFIG_DIR"):
        env["CLAUDE_CONFIG_DIR"] = os.environ["CCW_CLAUDE_CONFIG_DIR"]
    if spec.get("effort"):
        env["CLAUDE_CODE_EFFORT_LEVEL"] = str(spec["effort"])
    run = subprocess.run(
        argv,
        input=str(spec["prompt"]),
        capture_output=True,
        text=True,
        cwd=os.environ.get("CCW_WORKDIR") or None,
        env=env,
    )
    if run.returncode != 0:
        sys.stderr.write(run.stderr)
        return run.returncode
    emit({"output": {"text": run.stdout}})
    return 0


# ---------------------------------------------------------------- narrate


def narration_line(doc: dict[str, Any]) -> str | None:
    data = doc.get("data") or {}
    if doc.get("event") == "onAgentToolCall":
        agent = data.get("agent_type") or "main"
        return f"step tool={data.get('tool_name')} agent={agent}"
    if doc.get("event") == "onAgentResponse":
        text = " ".join(str(data.get("text", "")).split())
        return f"say {text[:SAY_CHARS]}"
    return None


def cmd_narrate() -> int:
    line = narration_line(envelope())
    if line:
        target = os.environ.get("NARRATION_LOG") or NARRATION_DEFAULT
        with open(target, "a", encoding="utf-8") as out:
            out.write(line + "\n")
    return 0


# ---------------------------------------------------------------- git credential helper


def cmd_git_credential(token_file: str) -> int:
    """`git credential get`: answer with the token read from `token_file`; ignore store/erase."""
    if (sys.argv[3:4] or ["get"])[0] != "get":
        return 0
    try:
        token = Path(token_file).read_text().strip()
    except OSError as exc:
        sys.stderr.write(f"git-credential: {exc}\n")
        return 1
    sys.stdout.write(f"username=x-access-token\npassword={token}\n")
    return 0


def main(argv: list[str]) -> int:
    sub = argv[1] if len(argv) > 1 else ""
    if sub == "pending-tasks":
        return cmd_pending_tasks()
    if sub == "review":
        return cmd_review()
    if sub == "narrate":
        return cmd_narrate()
    if sub == "git-credential" and len(argv) > 2:
        return cmd_git_credential(argv[2])
    sys.stderr.write("usage: mtg_hooks.py pending-tasks|review|narrate|git-credential FILE\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
