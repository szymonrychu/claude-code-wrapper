"""Render CLAUDE.md and copy extra config files into the session config dir."""

import shutil
from pathlib import Path, PurePosixPath

from claude_code_wrapper.config import ClaudeConfig, PromptsConfig

from .claude_json import atomic_write
from .errors import BootstrapError

MARKER = "<!-- written by claude-code-wrapper -->"


def write_instructions(config_dir: str | Path, prompts_cfg: PromptsConfig) -> Path | None:
    target = Path(config_dir) / "CLAUDE.md"
    parts: list[str] = []
    if prompts_cfg.global_claude_md:
        source = Path(prompts_cfg.global_claude_md)
        try:
            parts.append(source.read_text(encoding="utf-8"))
        except OSError as exc:
            raise BootstrapError(f"cannot read global_claude_md {source}: {exc}") from exc
    if prompts_cfg.system_append:
        parts.append(prompts_cfg.system_append)
    parts = [p for p in parts if p]
    if not parts:
        if target.is_file() and target.read_text(encoding="utf-8").startswith(MARKER):
            target.unlink()
        return None
    atomic_write(target, MARKER + "\n" + "\n\n".join(parts), mode=0o644)
    return target


def copy_config_files(config_dir: str | Path, claude_cfg: ClaudeConfig) -> None:
    root = Path(config_dir).resolve()
    for item in claude_cfg.config_files:
        if PurePosixPath(item.target).is_absolute() or Path(item.target).is_absolute():
            raise BootstrapError(f"config_files target must be relative: {item.target}")
        dest = (root / item.target).resolve()
        if not dest.is_relative_to(root) or dest == root:
            raise BootstrapError(f"config_files target escapes config dir: {item.target}")
        source = Path(item.source)
        if not source.exists():
            raise BootstrapError(f"config_files source not found: {item.source}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, dest, dirs_exist_ok=True)
        else:
            shutil.copyfile(source, dest)
