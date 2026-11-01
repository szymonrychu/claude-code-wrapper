"""mise tooling: render the global config, install tools, build the child's mise env."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Mapping
from pathlib import Path

from claude_code_wrapper.config import MiseConfig
from claude_code_wrapper.statusline import without_usage_token

log = logging.getLogger(__name__)

TAIL_BYTES = 2000


class ToolingError(Exception):
    pass


def _configured(cfg: MiseConfig) -> bool:
    return bool(cfg.config_file or cfg.tools)


def render_global_mise(mise_cfg: MiseConfig) -> Path | None:
    if not _configured(mise_cfg):
        return None
    target = Path(mise_cfg.global_config_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if mise_cfg.config_file:
        shutil.copyfile(mise_cfg.config_file, target)
    else:
        lines = ["[tools]"] + [f'{_key(n)} = "{_esc(v)}"' for n, v in mise_cfg.tools.items()]
        target.write_text("\n".join(lines) + "\n")
    return target


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _key(name: str) -> str:
    bare = name.replace("-", "").replace("_", "")
    return name if bare.isalnum() and name.isascii() else f'"{_esc(name)}"'


def mise_env(mise_cfg: MiseConfig) -> dict[str, str]:
    env: dict[str, str] = {}
    if _configured(mise_cfg):
        env["MISE_GLOBAL_CONFIG_FILE"] = mise_cfg.global_config_path
    env["MISE_TRUSTED_CONFIG_PATHS"] = ":".join(mise_cfg.trusted_paths)
    env["MISE_YES"] = "1"
    if "MISE_DATA_DIR" in os.environ:
        env["MISE_DATA_DIR"] = os.environ["MISE_DATA_DIR"]
    return env


async def _mise(args: list[str], *, cwd: Path | None, env: dict[str, str], timeout: float) -> None:
    binary = shutil.which("mise", path=env.get("PATH") or os.environ.get("PATH"))
    if binary is None:
        raise ToolingError("mise binary not found on PATH")
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            env=env,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise ToolingError(f"mise {' '.join(args)} timed out after {timeout}s") from exc
    except OSError as exc:
        raise ToolingError(f"mise {' '.join(args)}: {exc}") from exc
    if proc.returncode != 0:
        tail = out.decode("utf-8", "replace")[-TAIL_BYTES:]
        raise ToolingError(f"mise {' '.join(args)} exited {proc.returncode}: {tail}")


def _handle(cfg: MiseConfig, exc: ToolingError) -> None:
    if cfg.required:
        raise exc
    log.warning("mise step failed (not required): %s", exc)


async def install_global(mise_cfg: MiseConfig, env: Mapping[str, str]) -> None:
    if not (mise_cfg.install_on_start and _configured(mise_cfg)):
        return
    if not Path(mise_cfg.global_config_path).is_file():
        return
    full = without_usage_token(
        {
            **os.environ,
            **env,
            "MISE_GLOBAL_CONFIG_FILE": mise_cfg.global_config_path,
            "MISE_YES": "1",
        }
    )
    try:
        await _mise(["install"], cwd=None, env=full, timeout=mise_cfg.install_timeout_seconds)
    except ToolingError as exc:
        _handle(mise_cfg, exc)


async def install_workdir(mise_cfg: MiseConfig, workdir: Path, env: Mapping[str, str]) -> bool:
    if not mise_cfg.install_workdir:
        return False
    marker = next(
        (workdir / n for n in ("mise.toml", ".mise.toml") if (workdir / n).is_file()), None
    )
    if marker is None:
        return False
    full = without_usage_token({**os.environ, **env, "MISE_YES": "1"})
    if _configured(mise_cfg):
        full["MISE_GLOBAL_CONFIG_FILE"] = mise_cfg.global_config_path
    try:
        await _mise(
            ["trust", str(marker)], cwd=workdir, env=full, timeout=mise_cfg.install_timeout_seconds
        )
        await _mise(["install"], cwd=workdir, env=full, timeout=mise_cfg.install_timeout_seconds)
    except ToolingError as exc:
        _handle(mise_cfg, exc)
    return True
