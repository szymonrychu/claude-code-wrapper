"""In-flight inventory of a session: work that a clear or a move of the agent would lose.

Subagents, running background tasks and session crons (from the last Stop), the git state of
every repository under the workdir, and pod-local scratch files. Read only: git runs with
`GIT_OPTIONAL_LOCKS=0`, nothing is fetched and no file content is read.
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import os
import signal
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from claude_code_wrapper.redact import Redactor
from claude_code_wrapper.timeutil import iso, now_iso

GIT = "git"
MAX_DEPTH = 4
MAX_DIRTY_LINES = 50
MAX_SCRATCH = 100
SKIP_DIRS = frozenset({"node_modules", ".venv", ".git"})
PARALLEL_REPOS = 4

Worktree = dict[str, Any]
ScratchFile = dict[str, Any]


def find_repos(workdir: Path, deadline: float | None = None) -> list[Path]:
    """Directories holding a `.git` (dir or file) up to MAX_DEPTH below `workdir`. Past the
    monotonic `deadline` the walk raises TimeoutError: a partial list is never returned, so the
    caller reports the scan as timed out (unknown) even when no repository was found yet."""
    found: list[Path] = []

    def walk(directory: Path, depth: int) -> None:
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError("find_repos deadline")
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            return
        if ".git" in names:
            found.append(directory)
        if depth >= MAX_DEPTH:
            return
        for name in names:
            child = directory / name
            if name in SKIP_DIRS or child.is_symlink() or not child.is_dir():
                continue
            walk(child, depth + 1)

    walk(workdir, 0)
    return found


async def _git(repo: Path, *args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        GIT,
        *args,
        cwd=repo,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,  # the whole group is killed on timeout
    )
    try:
        out, _ = await proc.communicate()
    except BaseException:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        raise
    return proc.returncode or 0, out.decode("utf-8", "replace")


def _entry(workdir: Path, repo: Path, error: str | None) -> Worktree:
    rel = os.path.relpath(repo, workdir)
    return {
        "path": rel,
        "branch": None,
        "dirty": 0,
        "dirty_files": [],
        "ahead": None,
        "upstream": None,
        "error": error,
    }


async def _scan_repo(workdir: Path, repo: Path) -> Worktree:
    wt = _entry(workdir, repo, None)
    code, out = await _git(repo, "status", "--porcelain")
    if code != 0:
        wt["error"] = "git_failed"
        return wt
    lines = [ln for ln in out.splitlines() if ln]
    wt["dirty"] = len(lines)
    wt["dirty_files"] = lines[:MAX_DIRTY_LINES]
    code, out = await _git(repo, "symbolic-ref", "--short", "-q", "HEAD")
    wt["branch"] = out.strip() if code == 0 and out.strip() else None
    code, out = await _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if code != 0 or not out.strip():
        # `upstream is False` reads as "unpushed (no upstream)" to a client
        wt["upstream"] = False
        return wt
    wt["upstream"] = out.strip()
    code, out = await _git(repo, "rev-list", "--count", "@{u}..HEAD")
    if code != 0:
        wt["error"] = "git_failed"
        return wt
    wt["ahead"] = int(out.strip() or 0)
    return wt


async def scan_worktrees(workdir: Path, timeout: float) -> list[Worktree]:
    """Git state of every repository under `workdir`; repos not done in `timeout` seconds
    (one budget for all) are reported with error "timeout"."""
    start = time.monotonic()
    try:
        repos = await asyncio.wait_for(
            asyncio.to_thread(find_repos, workdir, start + timeout), timeout
        )
    except TimeoutError:
        return [_entry(workdir, workdir, "timeout")]
    gate = asyncio.Semaphore(PARALLEL_REPOS)

    async def one(repo: Path) -> Worktree:
        async with gate:
            return await _scan_repo(workdir, repo)

    tasks = {repo: asyncio.ensure_future(one(repo)) for repo in repos}
    if tasks:
        await asyncio.wait(tasks.values(), timeout=max(timeout - (time.monotonic() - start), 0))
    out: list[Worktree] = []
    for repo, task in tasks.items():
        if task.done() and not task.cancelled() and task.exception() is None:
            out.append(task.result())
            continue
        if not task.done():
            task.cancel()
        out.append(_entry(workdir, repo, "timeout" if not task.done() else "git_failed"))
    await asyncio.gather(*tasks.values(), return_exceptions=True)
    return out


def _scan_scratch(
    roots: list[str], since: datetime, deadline: float, exclude: Path | None
) -> tuple[list[ScratchFile], bool]:
    floor = since.timestamp()
    skip = os.path.realpath(exclude) if exclude is not None else None
    out: list[ScratchFile] = []
    incomplete = False

    def unreadable(_err: OSError) -> None:
        nonlocal incomplete
        incomplete = True

    for pattern in roots:
        for root in sorted(glob.glob(pattern)):
            if os.path.islink(root) or not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(
                root, onerror=unreadable, followlinks=False
            ):
                real = os.path.realpath(dirpath)
                if skip is not None and (real == skip or real.startswith(skip + os.sep)):
                    dirnames[:] = []
                    continue
                dirnames.sort()
                for name in sorted(filenames):
                    if len(out) >= MAX_SCRATCH:
                        return out, incomplete
                    if time.monotonic() > deadline:
                        return out, True
                    if name.endswith(".output") and os.path.basename(dirpath) == "tasks":
                        continue  # Claude Code's own background task output
                    path = os.path.join(dirpath, name)
                    try:
                        st = os.lstat(path)
                    except OSError:
                        incomplete = True
                        continue
                    if not os.path.isfile(path) or os.path.islink(path) or st.st_mtime < floor:
                        continue
                    mtime = iso(datetime.fromtimestamp(st.st_mtime, UTC))
                    out.append({"path": path, "size": st.st_size, "mtime": mtime})
            if time.monotonic() > deadline:
                return out, True
    return out, incomplete


async def scan_scratch(
    roots: list[str], since: datetime, timeout: float, exclude: Path | None = None
) -> tuple[list[ScratchFile], bool]:
    """Regular files modified at or after `since` under the glob `roots`, outside `exclude`
    (the workdir persists, so it is not scratch). At most MAX_SCRATCH, symlinks never followed,
    contents never read. The flag is true when the scan is incomplete (past `timeout` seconds
    or an unreadable directory): the files returned are then only part of the truth."""
    deadline = time.monotonic() + timeout
    return await asyncio.to_thread(_scan_scratch, roots, since, deadline, exclude)


_TASK_KEYS = ("id", "description", "status")
#: A cron also keeps its schedule (`schedule` or `cron`, whichever Claude Code sent) and prompt:
#: the successor after a clear must recreate it. Every other key stays dropped.
_CRON_KEYS = (*_TASK_KEYS, "schedule", "cron", "prompt")


def _slim(item: Any, keys: tuple[str, ...] = _TASK_KEYS) -> dict[str, Any]:
    """Whitelist `keys`: task text and commands are never output."""
    src = item if isinstance(item, dict) else {}
    return {k: src[k] for k in keys if k in src}


async def build_inflight(router: Any, workdir: Path, since: datetime, cfg: Any) -> dict[str, Any]:
    """The `GET /v1/sessions/{id}/inflight` body."""
    inventory = getattr(router, "stop_inventory", None) or {}
    ids = list(router.outstanding_subagent_ids)
    tasks = [
        t
        for t in inventory.get("background_tasks") or []
        if not isinstance(t, dict) or t.get("status", "running") == "running"
    ]
    worktrees, (scratch, scratch_incomplete) = await asyncio.gather(
        scan_worktrees(workdir, cfg.git_timeout_seconds),
        scan_scratch(cfg.scratch_roots, since, cfg.git_timeout_seconds, exclude=workdir),
    )
    redactor = getattr(router, "_redactor", None) or Redactor()
    crons = [
        redactor.redact_obj(_slim(c, _CRON_KEYS)) for c in inventory.get("session_crons") or []
    ]
    return {
        "subagents": len(ids),
        "subagent_ids": ids,
        "background_tasks": [redactor.redact_obj(_slim(t)) for t in tasks],
        "crons": crons,
        "worktrees": redactor.redact_obj(worktrees),
        "scratch": redactor.redact_obj(scratch),
        "unknown": scratch_incomplete
        or any(w["error"] in ("timeout", "git_failed") for w in worktrees),
        "stop_seen_at": inventory.get("at"),
        "scanned_at": now_iso(),
    }
