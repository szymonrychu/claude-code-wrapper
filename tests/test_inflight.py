"""In-flight inventory: git worktree scan, pod-local scratch scan, background tasks."""

from __future__ import annotations

import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from claude_code_wrapper import inflight
from claude_code_wrapper.config import PressureConfig
from claude_code_wrapper.inflight import build_inflight, scan_scratch, scan_worktrees

ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    (path / "a.txt").write_text("a")
    git(path, "add", "a.txt")
    git(path, "commit", "-q", "-m", "init")
    return path


def with_upstream(local: Path, remote: Path) -> None:
    git(remote.parent, "init", "-q", "--bare", "-b", "main", str(remote))
    git(local, "remote", "add", "origin", str(remote))
    git(local, "push", "-q", "-u", "origin", "main")


async def test_clean_repo_not_dirty_ahead_zero(tmp_path):
    work = tmp_path / "work"
    local = repo(work / "proj")
    with_upstream(local, tmp_path / "remote.git")
    (wt,) = await scan_worktrees(work, 20)
    assert wt == {
        "path": "proj",
        "branch": "main",
        "dirty": 0,
        "dirty_files": [],
        "ahead": 0,
        "upstream": "origin/main",
        "error": None,
    }


async def test_dirty_files_listed_and_capped_at_50(tmp_path):
    work = tmp_path / "work"
    local = repo(work / "proj")
    with_upstream(local, tmp_path / "remote.git")
    for i in range(60):
        (local / f"f{i:02}.txt").write_text("x")
    (wt,) = await scan_worktrees(work, 20)
    assert wt["dirty"] == 60
    assert len(wt["dirty_files"]) == 50
    assert wt["dirty_files"][0].startswith("?? f")


async def test_commits_ahead_of_upstream(tmp_path):
    work = tmp_path / "work"
    local = repo(work / "proj")
    with_upstream(local, tmp_path / "remote.git")
    (local / "b.txt").write_text("b")
    git(local, "add", "b.txt")
    git(local, "commit", "-q", "-m", "second")
    (wt,) = await scan_worktrees(work, 20)
    assert wt["ahead"] == 1 and wt["dirty"] == 0 and wt["error"] is None


async def test_branch_without_upstream_flagged(tmp_path):
    work = tmp_path / "work"
    repo(work / "proj")
    (wt,) = await scan_worktrees(work, 20)
    # the orchestrator gate reads `upstream is False` as "unpushed (no upstream)"
    assert wt["ahead"] is None and wt["upstream"] is False and wt["error"] is None
    assert wt["branch"] == "main"


async def test_nested_worktrees_found_depth_4(tmp_path):
    work = tmp_path / "work"
    repo(work / "a" / "b" / "c" / "d")  # depth 4: found
    repo(work / "a" / "b" / "c" / "d2" / "e")  # depth 5: not found
    repo(work / "top")
    paths = sorted(w["path"] for w in await scan_worktrees(work, 20))
    assert paths == ["a/b/c/d", "top"]


async def test_worktree_with_git_file_found(tmp_path):
    work = tmp_path / "work"
    main = repo(tmp_path / "main")
    git(main, "worktree", "add", "-q", "-b", "side", str(work / "wt"))
    (wt,) = await scan_worktrees(work, 20)
    assert wt["path"] == "wt" and wt["branch"] == "side"


async def test_skips_node_modules(tmp_path):
    work = tmp_path / "work"
    repo(work / "node_modules" / "pkg")
    repo(work / ".venv" / "lib")
    repo(work / "real")
    assert [w["path"] for w in await scan_worktrees(work, 20)] == ["real"]


async def test_timeout_marks_unscanned(tmp_path, monkeypatch):
    work = tmp_path / "work"
    repo(work / "one")
    repo(work / "two")
    slow = tmp_path / "slowgit"
    slow.write_text("#!/bin/sh\nsleep 30\n")
    slow.chmod(0o755)
    monkeypatch.setattr(inflight, "GIT", str(slow))
    started = time.monotonic()
    wts = await scan_worktrees(work, 0.5)
    assert time.monotonic() - started < 5
    assert sorted(w["path"] for w in wts) == ["one", "two"]
    assert all(w["error"] == "timeout" for w in wts)


async def test_git_failure_flagged(tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / "broken" / ".git").mkdir(parents=True)  # not a real repository
    (wt,) = await scan_worktrees(work, 20)
    assert wt["path"] == "broken" and wt["error"] == "git_failed"


# ------------------------------------------------------------------------------ scratch


def touch(path: Path, mtime: datetime | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("scratch")
    if mtime is not None:
        os.utime(path, (mtime.timestamp(), mtime.timestamp()))
    return path


def hour_ago() -> datetime:
    return datetime.now(UTC) - timedelta(hours=1)


async def test_scratch_outside_workdir_reported(tmp_path):
    f = touch(tmp_path / "pod" / "claude-1" / "scratchpad" / "notes.md")
    got = await scan_scratch([str(tmp_path / "pod" / "claude-*")], hour_ago(), 20)
    assert [g["path"] for g in got] == [str(f)]
    assert got[0]["size"] == 7 and got[0]["mtime"]


async def test_scratch_inside_workdir_excluded(tmp_path):
    work = tmp_path / "work"
    touch(work / "claude-1" / "persisted.md")
    touch(tmp_path / "pod" / "claude-1" / "a.md")
    got = await scan_scratch(
        [str(work / "claude-*"), str(tmp_path / "pod" / "claude-*")],
        hour_ago(),
        20,
        exclude=work,
    )
    assert [Path(g["path"]).name for g in got] == ["a.md"]


async def test_scratch_older_than_tenure_ignored(tmp_path):
    root = tmp_path / "pod" / "claude-1"
    touch(root / "old.md", datetime.now(UTC) - timedelta(days=2))
    new = touch(root / "new.md")
    got = await scan_scratch([str(root)], hour_ago(), 20)
    assert [g["path"] for g in got] == [str(new)]


async def test_scratch_capped_and_no_symlink_follow(tmp_path):
    root = tmp_path / "pod" / "claude-1"
    for i in range(130):
        touch(root / f"f{i:03}.txt")
    outside = tmp_path / "elsewhere"
    touch(outside / "secret.txt")
    (root / "link").symlink_to(outside, target_is_directory=True)
    (root / "flink.txt").symlink_to(outside / "secret.txt")
    got = await scan_scratch([str(root)], hour_ago(), 20)
    assert len(got) == 100
    assert not any("secret" in g["path"] or "link" in g["path"] for g in got)


# ------------------------------------------------------------------------------ assembly


class FakeRouter:
    def __init__(self, inventory):
        self.stop_inventory = inventory
        self.outstanding_subagent_ids = ["ag1", "ag2"]


async def test_background_tasks_only_running(tmp_path):
    inv = {
        "background_tasks": [
            {"id": "b1", "status": "running"},
            {"id": "b2", "status": "completed"},
            {"id": "b3"},
        ],
        "session_crons": [{"id": "c1"}],
        "at": "2026-01-01T00:00:00.000Z",
    }
    cfg = PressureConfig(scratch_roots=[])
    out = await build_inflight(FakeRouter(inv), tmp_path, hour_ago(), cfg)
    assert [t["id"] for t in out["background_tasks"]] == ["b1", "b3"]
    assert out["crons"] == [{"id": "c1"}]
    assert out["subagents"] == 2 and out["subagent_ids"] == ["ag1", "ag2"]
    assert out["stop_seen_at"] == "2026-01-01T00:00:00.000Z"
    assert out["unknown"] is False and out["scratch"] == [] and out["worktrees"] == []


async def test_unknown_when_a_scan_times_out(tmp_path, monkeypatch):
    repo(tmp_path / "work" / "p")
    slow = tmp_path / "slowgit"
    slow.write_text("#!/bin/sh\nsleep 30\n")
    slow.chmod(0o755)
    monkeypatch.setattr(inflight, "GIT", str(slow))
    cfg = PressureConfig(scratch_roots=[], git_timeout_seconds=0.3)
    out = await build_inflight(FakeRouter(None), tmp_path / "work", hour_ago(), cfg)
    assert out["unknown"] is True and out["stop_seen_at"] is None
    assert out["background_tasks"] == [] and out["crons"] == []


async def test_scratch_roots_default():
    assert PressureConfig().scratch_roots == ["/tmp/claude-*"]  # noqa: S108
