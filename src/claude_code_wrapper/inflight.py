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

from claude_code_wrapper.timeutil import iso, now_iso

GIT = "git"
MAX_DEPTH = 4
MAX_DIRTY_LINES = 50
MAX_SCRATCH = 100
SKIP_DIRS = frozenset({"node_modules", ".venv", ".git"})
PARALLEL_REPOS = 4

Worktree = dict[str, Any]
ScratchFile = dict[str, Any]


def find_repos(workdir: Path) -> list[Path]:
    """Directories holding a `.git` (dir or file) up to MAX_DEPTH below `workdir`."""
    found: list[Path] = []

    def walk(directory: Path, depth: int) -> None:
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
        # the orchestrator gate reads `upstream is False` as "unpushed (no upstream)"
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
    repos = await asyncio.to_thread(find_repos, workdir)
    gate = asyncio.Semaphore(PARALLEL_REPOS)

    async def one(repo: Path) -> Worktree:
        async with gate:
            return await _scan_repo(workdir, repo)

    tasks = {repo: asyncio.ensure_future(one(repo)) for repo in repos}
    if tasks:
        await asyncio.wait(tasks.values(), timeout=timeout)
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
) -> list[ScratchFile]:
    floor = since.timestamp()
    skip = os.path.realpath(exclude) if exclude is not None else None
    out: list[ScratchFile] = []
    for pattern in roots:
        for root in sorted(glob.glob(pattern)):
            if os.path.islink(root) or not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                real = os.path.realpath(dirpath)
                if skip is not None and (real == skip or real.startswith(skip + os.sep)):
                    dirnames[:] = []
                    continue
                dirnames.sort()
                for name in sorted(filenames):
                    if time.monotonic() > deadline or len(out) >= MAX_SCRATCH:
                        return out
                    path = os.path.join(dirpath, name)
                    try:
                        st = os.lstat(path)
                    except OSError:
                        continue
                    if not os.path.isfile(path) or os.path.islink(path) or st.st_mtime < floor:
                        continue
                    mtime = iso(datetime.fromtimestamp(st.st_mtime, UTC))
                    out.append({"path": path, "size": st.st_size, "mtime": mtime})
    return out


async def scan_scratch(
    roots: list[str], since: datetime, timeout: float, exclude: Path | None = None
) -> list[ScratchFile]:
    """Regular files modified at or after `since` under the glob `roots`, outside `exclude`
    (the workdir persists, so it is not scratch). At most MAX_SCRATCH, symlinks never followed,
    contents never read; a scan past `timeout` seconds returns what it has."""
    deadline = time.monotonic() + timeout
    return await asyncio.to_thread(_scan_scratch, roots, since, deadline, exclude)


async def build_inflight(router: Any, workdir: Path, since: datetime, cfg: Any) -> dict[str, Any]:
    """The `GET /v1/sessions/{id}/inflight` body."""
    inventory = getattr(router, "stop_inventory", None) or {}
    ids = list(router.outstanding_subagent_ids)
    tasks = [
        t
        for t in inventory.get("background_tasks") or []
        if not isinstance(t, dict) or t.get("status", "running") == "running"
    ]
    worktrees, scratch = await asyncio.gather(
        scan_worktrees(workdir, cfg.git_timeout_seconds),
        scan_scratch(cfg.scratch_roots, since, cfg.git_timeout_seconds, exclude=workdir),
    )
    return {
        "subagents": len(ids),
        "subagent_ids": ids,
        "background_tasks": tasks,
        "crons": list(inventory.get("session_crons") or []),
        "worktrees": worktrees,
        "scratch": scratch,
        "unknown": any(w["error"] in ("timeout", "git_failed") for w in worktrees),
        "stop_seen_at": inventory.get("at"),
        "scanned_at": now_iso(),
    }
