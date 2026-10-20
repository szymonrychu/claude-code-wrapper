from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from claude_code_wrapper.config import InitConfig
from claude_code_wrapper.workdir import (
    FileWriteError,
    WorkdirError,
    prepare_workdir,
    run_setup_commands,
    safe_write_files,
    sanitize_name,
    write_gitconfig,
)


def git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(cwd.parent / "g.cfg"),
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    return subprocess.run(
        ["git", "-c", "protocol.file.allow=always", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    cfg = tmp_path / "gitcfg"
    cfg.write_text(
        '[user]\n\tname = T\n\temail = t@e.x\n[protocol "file"]\n\tallow = always\n'
        "[safe]\n\tdirectory = *\n"
    )
    return {**os.environ, "GIT_CONFIG_GLOBAL": str(cfg), "GIT_CONFIG_SYSTEM": "/dev/null"}


@pytest.fixture
def origin(tmp_path: Path, env: dict[str, str]) -> Path:
    sub = tmp_path / "subsrc"
    sub.mkdir()
    for d in (sub,):
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=d, env=env, check=True)
    (sub / "s.txt").write_text("s")
    subprocess.run(["git", "add", "."], cwd=sub, env=env, check=True)
    subprocess.run(["git", "commit", "-qm", "s"], cwd=sub, env=env, check=True)
    o = tmp_path / "origin"
    o.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=o, env=env, check=True)
    (o / "a.txt").write_text("a")
    subprocess.run(["git", "add", "."], cwd=o, env=env, check=True)
    subprocess.run(["git", "commit", "-qm", "a"], cwd=o, env=env, check=True)
    subprocess.run(
        ["git", "submodule", "add", "-q", str(sub), "vendor"], cwd=o, env=env, check=True
    )
    subprocess.run(["git", "commit", "-qm", "sub"], cwd=o, env=env, check=True)
    subprocess.run(["git", "branch", "feat"], cwd=o, env=env, check=True)
    return o


def cfg(**wd: object) -> InitConfig:
    return InitConfig.model_validate({"workdir": wd})


def test_sanitize_ok() -> None:
    assert sanitize_name("My-proj_1.2") == "My-proj_1.2"
    assert sanitize_name("a" * 64) == "a" * 64


@pytest.mark.parametrize("bad", ["", ".", "..", ".hidden", "a/b", "a b", "é", "a" * 65, "a\x00"])
def test_sanitize_bad(bad: str) -> None:
    with pytest.raises(WorkdirError):
        sanitize_name(bad)


async def test_empty_mode_creates_dir(tmp_path: Path, env: dict[str, str]) -> None:
    root = tmp_path / "root"
    p = await prepare_workdir(cfg(mode="empty"), root, session_id="sid1", env=env)
    assert p == root / "sid1" and p.is_dir()
    p = await prepare_workdir(cfg(mode="empty", name="cfgname"), root, session_id="sid1", env=env)
    assert p == root / "cfgname"
    p = await prepare_workdir(
        cfg(mode="empty", name="cfgname"), root, session_id="s", name="req", env=env
    )
    assert p == root / "req"
    (p / "keep").write_text("x")
    again = await prepare_workdir(cfg(mode="empty"), root, session_id="s", name="req", env=env)
    assert (again / "keep").exists()
    with pytest.raises(WorkdirError):
        await prepare_workdir(cfg(mode="empty"), root, session_id="s", name="../x", env=env)


async def test_mounted_mode_requires_existing_dir(tmp_path: Path, env: dict[str, str]) -> None:
    m = tmp_path / "mnt"
    m.mkdir()
    (m / "f").write_text("1")
    p = await prepare_workdir(
        cfg(mode="mounted", path=str(m)), tmp_path / "r", session_id="s", env=env
    )
    assert p == m and sorted(x.name for x in m.iterdir()) == ["f"]
    o = tmp_path / "other"
    o.mkdir()
    p = await prepare_workdir(
        cfg(mode="mounted", path=str(m)),
        tmp_path / "r",
        session_id="s",
        path_override=str(o),
        env=env,
    )
    assert p == o
    with pytest.raises(WorkdirError):
        await prepare_workdir(
            cfg(mode="mounted", path=str(m)),
            tmp_path / "r",
            session_id="s",
            path_override=str(tmp_path / "missing"),
            env=env,
        )
    with pytest.raises(WorkdirError):
        await prepare_workdir(
            cfg(mode="mounted", path=str(m)),
            tmp_path / "r",
            session_id="s",
            path_override="rel",
            env=env,
        )
    f = tmp_path / "file"
    f.write_text("x")
    with pytest.raises(WorkdirError):
        await prepare_workdir(
            cfg(mode="mounted", path=str(f)), tmp_path / "r", session_id="s", env=env
        )


async def test_clone_with_submodules(tmp_path: Path, origin: Path, env: dict[str, str]) -> None:
    root = tmp_path / "root"
    c = cfg(mode="clone", repo_url=str(origin), name="proj", submodules=True)
    p = await prepare_workdir(c, root, session_id="s", env=env)
    assert p == root / "proj" and (p / "a.txt").exists()
    assert (p / "vendor" / "s.txt").exists()
    (p / "dirty").write_text("keep")
    p2 = await prepare_workdir(c, root, session_id="s", env=env)
    assert p2 == p and (p / "dirty").read_text() == "keep"


async def test_clone_mode_clones_branch(tmp_path: Path, origin: Path, env: dict[str, str]) -> None:
    c = cfg(mode="clone", repo_url=f"file://{origin}", name="p", depth=1, branch="feat")
    p = await prepare_workdir(c, tmp_path / "r", session_id="s", env=env)
    assert git(p, "rev-parse", "--abbrev-ref", "HEAD") == "feat"
    assert git(p, "rev-list", "--count", "HEAD") == "1"
    assert not (p / "vendor" / "s.txt").exists()


async def test_clone_rejects_foreign_dir(tmp_path: Path, origin: Path, env: dict[str, str]) -> None:
    root = tmp_path / "r"
    (root / "plain").mkdir(parents=True)
    c = cfg(mode="clone", repo_url=str(origin), name="plain")
    with pytest.raises(WorkdirError):
        await prepare_workdir(c, root, session_id="s", env=env)
    await prepare_workdir(
        cfg(mode="clone", repo_url=str(origin), name="ok"), root, session_id="s", env=env
    )
    with pytest.raises(WorkdirError):
        await prepare_workdir(
            cfg(mode="clone", repo_url=str(tmp_path / "different"), name="ok"),
            root,
            session_id="s",
            env=env,
        )


async def test_clone_error_redacts_credentials(tmp_path: Path, env: dict[str, str]) -> None:
    c = cfg(mode="clone", repo_url="https://user:s3cretpass@127.0.0.1:1/x.git", name="p")
    with pytest.raises(WorkdirError) as ei:
        await prepare_workdir(c, tmp_path / "r", session_id="s", env=env)
    assert "s3cretpass" not in str(ei.value)


def test_gitconfig_written(tmp_path: Path) -> None:
    from claude_code_wrapper.config import GitConfig

    wd = tmp_path / "wd"
    p = write_gitconfig(tmp_path, GitConfig(user_name="Bot", user_email="b@x.y"), wd)
    assert p == tmp_path / "gitconfig"
    text = p.read_text()
    assert "name = Bot" in text and "email = b@x.y" in text and f"directory = {wd}" in text
    p = write_gitconfig(tmp_path, GitConfig(), wd)
    assert "[user]" not in p.read_text() and "[safe]" in p.read_text()


async def test_setup_commands_run_once_and_fail_with_tail(tmp_path: Path) -> None:
    from claude_code_wrapper.config import SetupCommand

    wd = tmp_path / "wd"
    wd.mkdir()
    marker = tmp_path / "marker"
    cmds = [SetupCommand(run="echo a >> out"), SetupCommand(run="echo $FOO >> out")]
    await run_setup_commands(cmds, wd, {"PATH": os.environ["PATH"], "FOO": "bar"}, marker)
    assert (wd / "out").read_text() == "a\nbar\n" and marker.exists()
    await run_setup_commands(cmds, wd, {"PATH": os.environ["PATH"]}, marker)
    assert (wd / "out").read_text() == "a\nbar\n"
    marker = tmp_path / "marker2"
    cmds = [
        SetupCommand(run="true"),
        SetupCommand(run="echo Bearer abcdefghijklmnop; echo boom >&2; exit 3"),
    ]
    with pytest.raises(WorkdirError) as ei:
        await run_setup_commands(cmds, wd, {"PATH": os.environ["PATH"]}, marker)
    msg = str(ei.value)
    assert "1" in msg and "3" in msg and "boom" in msg and "abcdefghijklmnop" not in msg
    assert not marker.exists()


async def test_setup_timeout_kills_group(tmp_path: Path) -> None:
    from claude_code_wrapper.config import SetupCommand

    wd = tmp_path / "wd"
    wd.mkdir()
    cmd = SetupCommand(run="(sleep 1; touch late) & sleep 30", timeout_seconds=0.3)
    with pytest.raises(WorkdirError, match="timed out"):
        await run_setup_commands([cmd], wd, {"PATH": os.environ["PATH"]}, tmp_path / "m")
    import asyncio

    await asyncio.sleep(1.5)
    assert not (wd / "late").exists()


def test_safe_write_files(tmp_path: Path) -> None:
    wd = tmp_path / "wd"
    wd.mkdir()
    out = safe_write_files(wd, {"a.txt": "x", "d/e/f.txt": "y"}, max_bytes=10, max_files=5)
    assert sorted(out) == ["a.txt", "d/e/f.txt"]
    assert (wd / "d/e/f.txt").read_text() == "y"


@pytest.mark.parametrize("path", ["/etc/x", "../x", "a/../../x", "", "a\x00b"])
def test_safe_write_invalid_paths(tmp_path: Path, path: str) -> None:
    wd = tmp_path / "wd"
    wd.mkdir()
    with pytest.raises(FileWriteError) as ei:
        safe_write_files(wd, {path: "x"}, max_bytes=10, max_files=5)
    assert ei.value.code == "invalid_path"


def test_safe_write_rejects_escape_and_symlink(tmp_path: Path) -> None:
    wd = tmp_path / "wd"
    wd.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (wd / "link").symlink_to(outside)
    with pytest.raises(FileWriteError) as ei:
        safe_write_files(wd, {"link/x.txt": "x"}, max_bytes=10, max_files=5)
    assert ei.value.code == "invalid_path"
    assert not (outside / "x.txt").exists()
    (wd / "flink").symlink_to(outside / "t.txt")
    with pytest.raises(FileWriteError):
        safe_write_files(wd, {"flink": "x"}, max_bytes=10, max_files=5)


def test_safe_write_caps(tmp_path: Path) -> None:
    wd = tmp_path / "wd"
    wd.mkdir()
    with pytest.raises(FileWriteError) as ei:
        safe_write_files(wd, {"a": "é" * 6}, max_bytes=10, max_files=5)
    assert ei.value.code == "too_large"
    with pytest.raises(FileWriteError) as ei:
        safe_write_files(wd, {"a": "1", "b": "2"}, max_bytes=10, max_files=1)
    assert ei.value.code == "too_large"
    assert not (wd / "a").exists()


def test_name_sanitised() -> None:
    assert sanitize_name("ok-1._x") == "ok-1._x"
    for bad in ("../x", "a/b", ".hidden", "..", ".", "", "x" * 65, "a b"):
        with pytest.raises(WorkdirError):
            sanitize_name(bad)


async def test_clone_reuses_existing_same_origin(
    tmp_path: Path, origin: Path, env: dict[str, str]
) -> None:
    root = tmp_path / "root"
    c = cfg(mode="clone", repo_url=str(origin), name="proj")
    p = await prepare_workdir(c, root, session_id="s", env=env)
    (p / "dirty").write_text("keep")
    p2 = await prepare_workdir(c, root, session_id="s", env=env)
    assert p2 == p and (p / "dirty").read_text() == "keep"


async def test_clone_error_redacts_credentials_with_depth_branch(
    tmp_path: Path, env: dict[str, str]
) -> None:
    c = cfg(
        mode="clone",
        repo_url="https://user:s3cretpass@127.0.0.1:1/x.git",
        name="p",
        depth=1,
        branch="feature-branch",
    )
    with pytest.raises(WorkdirError) as ei:
        await prepare_workdir(c, tmp_path / "r", session_id="s", env=env)
    assert "s3cr" not in str(ei.value)


async def test_setup_signalled_is_not_timeout(tmp_path: Path) -> None:
    from claude_code_wrapper.config import SetupCommand

    wd = tmp_path / "wd"
    wd.mkdir()
    cmd = SetupCommand(run="kill -HUP $$", timeout_seconds=30)
    with pytest.raises(WorkdirError) as ei:
        await run_setup_commands([cmd], wd, {"PATH": os.environ["PATH"]}, tmp_path / "m")
    assert "timed out" not in str(ei.value)
