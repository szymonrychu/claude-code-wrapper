"""Pre-install Claude Code plugins and marketplaces through the `claude plugin` CLI."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from claude_code_wrapper.config import PluginsConfig
from claude_code_wrapper.redact import Redactor

log = logging.getLogger(__name__)

TAIL_BYTES = 2000
OFFICIAL_MARKETPLACE = "claude-plugins-official"
OFFICIAL_SOURCE = "anthropics/claude-plugins-official"


class PluginInstallError(Exception):
    def __init__(self, plugin_id: str, exit_code: int | None, output_tail: str) -> None:
        super().__init__(f"plugin step {plugin_id!r} failed (exit {exit_code}): {output_tail}")
        self.plugin_id = plugin_id
        self.exit_code = exit_code
        self.output_tail = output_tail


@dataclass
class PluginReport:
    installed: list[str] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _is_local_path(source: str) -> bool:
    return source.startswith(("/", "./", "../", "~")) or source in (".", "..")


async def _run(
    label: str,
    args: list[str],
    *,
    env: dict[str, str],
    workdir: Path,
    timeout: float,
    redactor: Redactor,
) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=workdir,
            env=env,
        )
    except OSError as exc:
        raise PluginInstallError(label, None, redactor.redact(str(exc))) from exc
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise PluginInstallError(label, None, f"timed out after {timeout}s") from exc
    text = redactor.redact(out.decode("utf-8", "replace"))
    if proc.returncode != 0:
        raise PluginInstallError(label, proc.returncode, text[-TAIL_BYTES:])
    return text


def _ids(text: str, key: str, label: str) -> set[str]:
    try:
        data = json.loads(text)
        return {str(item[key]) for item in data}
    except (ValueError, KeyError, TypeError) as exc:
        raise PluginInstallError(label, 0, f"unparseable json: {text[-TAIL_BYTES:]}") from exc


async def install_plugins(
    plugins_cfg: PluginsConfig,
    *,
    claude_binary: str,
    config_dir: Path,
    workdir: Path,
    env: Mapping[str, str],
) -> PluginReport:
    report = PluginReport()
    sources: dict[str, str] = {}
    for m in plugins_cfg.marketplaces:
        sources[m.name] = m.source
        if _is_local_path(m.source):
            # claude runs with cwd=workdir, so relative paths resolve against it
            path = (workdir / Path(m.source).expanduser()).resolve()
            if not path.is_dir():
                raise PluginInstallError(
                    m.name, None, f"marketplace source {m.source!r} is not a directory"
                )
            sources[m.name] = str(path)

    full = {**env, "CLAUDE_CONFIG_DIR": str(config_dir), "DISABLE_AUTOUPDATER": "1"}
    redactor = Redactor(env.values())

    async def run(label: str, *args: str) -> str:
        return await _run(
            label,
            [claude_binary, *args],
            env=full,
            workdir=workdir,
            timeout=plugins_cfg.timeout_seconds,
            redactor=redactor,
        )

    known = _ids(
        await run("marketplace list", "plugin", "marketplace", "list", "--json"),
        "name",
        "marketplace list",
    )
    for m in plugins_cfg.marketplaces:
        if m.name in known:
            continue
        await run(
            m.name, "plugin", "marketplace", "add", sources[m.name], "--scope", "user", "--json"
        )

    # `plugin install` resolves against the marketplace copy on disk, which goes stale in a
    # long-lived config dir; refresh every referenced marketplace first (a no-op when current).
    referenced = dict.fromkeys(
        i.plugin.rpartition("@")[2] for i in plugins_cfg.install if "@" in i.plugin
    )
    for name in referenced:
        try:
            await run(name, "plugin", "marketplace", "update", name)
        except PluginInstallError as exc:
            if name == OFFICIAL_MARKETPLACE and name not in known:
                await run(
                    name,
                    "plugin",
                    "marketplace",
                    "add",
                    OFFICIAL_SOURCE,
                    "--scope",
                    "user",
                    "--json",
                )
                continue
            # best-effort: the local copy may serve the install, which reports its own failure
            log.warning("marketplace %s update failed, using the local copy: %s", name, exc)
            report.warnings.append(f"marketplace {name} update failed: {exc}")

    failed: set[str] = set()
    present = _ids(await run("plugin list", "plugin", "list", "--json"), "id", "plugin list")
    for item in plugins_cfg.install:
        if item.plugin in present:
            report.already_present.append(item.plugin)
            continue
        try:
            await run(
                item.plugin, "plugin", "install", item.plugin, "-s", item.scope, "-y", "--json"
            )
        except PluginInstallError as exc:
            if item.required:
                raise
            report.warnings.append(str(exc))
            failed.add(item.plugin)
            log.warning("optional plugin %s failed: %s", item.plugin, exc)
            continue
        report.installed.append(item.plugin)

    final = _ids(await run("plugin list", "plugin", "list", "--json"), "id", "plugin list")
    for item in plugins_cfg.install:
        if item.plugin not in final and item.plugin not in failed:
            msg = "not present in `claude plugin list` after install"
            if item.required:
                raise PluginInstallError(item.plugin, 0, msg)
            if item.plugin in report.installed:
                report.installed.remove(item.plugin)
            report.warnings.append(f"{item.plugin}: {msg}")
    return report
