from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from claude_code_wrapper.config import PluginsConfig
from claude_code_wrapper.plugins import PluginInstallError, install_plugins

ID = "workbench@wb"


@pytest.fixture
def log(tmp_path, monkeypatch) -> Path:
    p = tmp_path / "fake.log"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(p))
    return p


def entries(log: Path) -> list[dict]:
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def argvs(log: Path) -> list[list[str]]:
    return [e["argv"] for e in entries(log)]


def marketplace(root: Path, name: str = "wb") -> Path:
    d = root / name
    (d / ".claude-plugin").mkdir(parents=True)
    (d / ".claude-plugin" / "marketplace.json").write_text(json.dumps({"name": name}))
    return d


def cfg(source: str, **kw) -> PluginsConfig:
    return PluginsConfig.model_validate(
        {"marketplaces": [{"name": "wb", "source": source}], "install": [{"plugin": ID, **kw}]}
    )


async def go(c, claude, tmp_path, env=None):
    conf = tmp_path / "conf"
    conf.mkdir(exist_ok=True)
    wd = tmp_path / "wd"
    wd.mkdir(exist_ok=True)
    full = {k: os.environ[k] for k in ("FAKE_CLAUDE_LOG", "PATH") if k in os.environ}
    report = await install_plugins(
        c, claude_binary=str(claude), config_dir=conf, workdir=wd, env={**full, **(env or {})}
    )
    return report, conf


async def test_adds_missing_marketplace_and_installs(fake_claude_bin, tmp_path, log):
    mk = marketplace(tmp_path)
    report, _ = await go(cfg(str(mk)), fake_claude_bin, tmp_path)
    assert report.installed == [ID]
    assert argvs(log) == [
        ["plugin", "marketplace", "list", "--json"],
        ["plugin", "marketplace", "add", str(mk), "--scope", "user", "--json"],
        ["plugin", "list", "--json"],
        ["plugin", "install", ID, "-s", "user", "-y", "--json"],
        ["plugin", "list", "--json"],
    ]


async def test_idempotent_second_run_no_commands(fake_claude_bin, tmp_path, log):
    c = cfg(str(marketplace(tmp_path)))
    await go(c, fake_claude_bin, tmp_path)
    n = len(entries(log))
    report, _ = await go(c, fake_claude_bin, tmp_path)
    assert report.installed == [] and report.already_present == [ID]
    new = argvs(log)[n:]
    assert new == [
        ["plugin", "marketplace", "list", "--json"],
        ["plugin", "list", "--json"],
        ["plugin", "list", "--json"],
    ]


async def test_required_failure_raises_with_tail(fake_claude_bin, tmp_path, log):
    c = cfg(str(marketplace(tmp_path)))
    env = {"FAKE_CLAUDE_PLUGIN_FAIL": ID, "API_TOKEN": "workbench@wb"}
    with pytest.raises(PluginInstallError) as ei:
        await go(c, fake_claude_bin, tmp_path, env)
    e = ei.value
    assert e.plugin_id == ID and e.exit_code == 1
    assert "failed to install" in e.output_tail and ID not in e.output_tail


async def test_optional_failure_warns(fake_claude_bin, tmp_path, log):
    c = cfg(str(marketplace(tmp_path)), required=False)
    report, _ = await go(c, fake_claude_bin, tmp_path, {"FAKE_CLAUDE_PLUGIN_FAIL": ID})
    assert report.installed == [] and len(report.warnings) == 1


async def test_local_marketplace_dir_must_exist(fake_claude_bin, tmp_path, log):
    with pytest.raises(PluginInstallError):
        await go(cfg(str(tmp_path / "nope")), fake_claude_bin, tmp_path)
    assert entries(log) == []


async def test_relative_marketplace_resolved_against_workdir(fake_claude_bin, tmp_path, log):
    wd = tmp_path / "wd"
    wd.mkdir()
    mk = marketplace(wd)
    report, _ = await go(cfg("./wb"), fake_claude_bin, tmp_path)
    assert report.installed == [ID]
    assert argvs(log)[1][3] == str(mk.resolve())
    with pytest.raises(PluginInstallError):
        await go(cfg("../elsewhere"), fake_claude_bin, tmp_path)


async def test_config_dir_env_passed(fake_claude_bin, tmp_path, log):
    _, conf = await go(cfg(str(marketplace(tmp_path))), fake_claude_bin, tmp_path)
    es = entries(log)
    assert es and all(e["env"]["CLAUDE_CONFIG_DIR"] == str(conf) for e in es)
    assert all(Path(e["cwd"]).resolve() == (tmp_path / "wd").resolve() for e in es)
    assert (conf / "plugins" / "fake-state.json").exists()


async def test_timeout(tmp_path):
    slow = tmp_path / "slow"
    slow.write_text("#!/bin/sh\nsleep 5\n")
    slow.chmod(0o755)
    c = PluginsConfig.model_validate(
        {"marketplaces": [{"name": "m", "source": "o/r"}], "timeout_seconds": 0.2}
    )
    with pytest.raises(PluginInstallError):
        await install_plugins(
            c, claude_binary=str(slow), config_dir=tmp_path, workdir=tmp_path, env={}
        )
