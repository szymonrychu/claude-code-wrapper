"""Session workdir preparation, git config, setup commands and the turn file guard."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
from collections.abc import Mapping, Sequence
from pathlib import Path

from .config import GitConfig, InitConfig, SetupCommand
from .redact import Redactor

_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
MAX_NAME_LEN = 64
OUTPUT_TAIL = 2000
_USERINFO = re.compile(r"(://)[^/\s@]+@")
_REDACTOR = Redactor()


class WorkdirError(Exception):
    """Workdir preparation or setup failure; the message is already redacted."""


class FileWriteError(Exception):
    """Rejected turn file write; `code` is `too_large` or `invalid_path`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def redact_output(text: str) -> str:
    """Mask credentials in URLs and well-known token shapes."""
    return str(_REDACTOR.redact(_USERINFO.sub(r"\1***@", text)))


def sanitize_name(name: str) -> str:
    if not name or len(name) > MAX_NAME_LEN or name.startswith(".") or not _NAME_RE.fullmatch(name):
        raise WorkdirError(f"invalid workdir name {name!r}")
    return name


async def _run(
    argv: Sequence[str], cwd: Path | None, env: Mapping[str, str], timeout: float | None = None
) -> tuple[int, str, bool]:
    """Run a process in its own group; returns (exit code, output, timed_out)."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=dict(env),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        out, _ = await proc.communicate()
        return proc.returncode or 0, out.decode(errors="replace"), True
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        raise
    return proc.returncode or 0, out.decode(errors="replace"), False


async def _git(args: Sequence[str], cwd: Path | None, env: Mapping[str, str]) -> str:
    git_env = {**env, "GIT_TERMINAL_PROMPT": "0"}
    try:
        code, out, _ = await _run(["git", *args], cwd, git_env)
    except FileNotFoundError as exc:
        raise WorkdirError("git is not installed") from exc
    if code != 0:
        raise WorkdirError(f"git failed ({code}): {redact_output(out)[-OUTPUT_TAIL:]}")
    return out.strip()


async def prepare_workdir(
    init_cfg: InitConfig,
    workdir_root: str | Path,
    *,
    session_id: str,
    name: str | None = None,
    path_override: str | None = None,
    env: Mapping[str, str],
) -> Path:
    wd = init_cfg.workdir
    root = Path(workdir_root)
    if wd.mode == "mounted":
        raw = path_override or wd.path
        path = Path(raw)
        if not path.is_absolute():
            raise WorkdirError(f"mounted workdir path must be absolute: {raw!r}")
        if not path.is_dir():
            raise WorkdirError(f"mounted workdir does not exist: {raw!r}")
        return path

    target = root / sanitize_name(name or wd.name or session_id)
    if wd.mode == "empty":
        target.mkdir(parents=True, exist_ok=True)
        return target

    # clone
    if not target.exists():
        root.mkdir(parents=True, exist_ok=True)
        args = ["clone"]
        if wd.depth > 0:
            args += ["--depth", str(wd.depth)]
        if wd.branch:
            args += ["--branch", wd.branch]
        args += [wd.repo_url, str(target)]
        await _git(args, None, env)
        if wd.submodules:
            await _git(["submodule", "update", "--init", "--recursive"], target, env)
        return target
    if not (target / ".git").exists():
        raise WorkdirError(f"{target} exists and is not a git repository")
    try:
        origin = await _git(["remote", "get-url", "origin"], target, env)
    except WorkdirError as exc:
        raise WorkdirError(f"{target} has no origin remote: {exc}") from exc
    if origin != wd.repo_url:
        raise WorkdirError(
            f"{target} is a clone of {redact_output(origin)}, not {redact_output(wd.repo_url)}"
        )
    return target


def write_gitconfig(session_dir: str | Path, git_cfg: GitConfig, workdir: str | Path) -> Path:
    lines: list[str] = []
    if git_cfg.user_name or git_cfg.user_email:
        lines.append("[user]")
        if git_cfg.user_name:
            lines.append(f"\tname = {git_cfg.user_name}")
        if git_cfg.user_email:
            lines.append(f"\temail = {git_cfg.user_email}")
    lines += ["[safe]", f"\tdirectory = {workdir}"]
    path = Path(session_dir) / "gitconfig"
    path.write_text("\n".join(lines) + "\n")
    return path


async def run_setup_commands(
    commands: Sequence[SetupCommand],
    workdir: str | Path,
    env: Mapping[str, str],
    marker_path: str | Path,
) -> None:
    marker = Path(marker_path)
    if marker.exists():
        return
    for index, cmd in enumerate(commands):
        code, out, timed_out = await _run(
            ["sh", "-c", cmd.run], Path(workdir), env, cmd.timeout_seconds
        )
        tail = redact_output(out)[-OUTPUT_TAIL:]
        if timed_out:
            raise WorkdirError(
                f"setup command {index} timed out after {cmd.timeout_seconds}s: {tail}"
            )
        if code != 0:
            raise WorkdirError(f"setup command {index} failed with exit code {code}: {tail}")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("done\n")


def safe_write_files(
    workdir: str | Path, files: Mapping[str, str], *, max_bytes: int, max_files: int
) -> list[str]:
    if len(files) > max_files:
        raise FileWriteError("too_large", f"more than {max_files} files")
    root = Path(os.path.realpath(workdir))
    planned: list[tuple[str, Path, bytes]] = []
    for rel, content in files.items():
        data = content.encode("utf-8")
        if len(data) > max_bytes:
            raise FileWriteError("too_large", f"{rel!r} exceeds {max_bytes} bytes")
        if not rel or "\x00" in rel or os.path.isabs(rel):
            raise FileWriteError("invalid_path", f"invalid path {rel!r}")
        resolved = Path(os.path.realpath(root / rel))
        if resolved == root or not resolved.is_relative_to(root):
            raise FileWriteError("invalid_path", f"path escapes workdir: {rel!r}")
        if resolved.is_dir() or any(p.exists() and not p.is_dir() for p in resolved.parents):
            raise FileWriteError("invalid_path", f"path conflicts with existing entry: {rel!r}")
        planned.append((rel, resolved, data))
    for rel, resolved, data in planned:
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_bytes(data)
        except OSError as exc:
            raise FileWriteError("invalid_path", f"cannot write {rel!r}: {exc.strerror}") from exc
    return [rel for rel, _, _ in planned]
