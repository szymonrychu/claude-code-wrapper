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
        ["plugin", "marketplace", "update", "wb"],
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
        ["plugin", "marketplace", "update", "wb"],
        ["plugin", "list", "--json"],
        ["plugin", "list", "--json"],
    ]


OFFICIAL = "claude-plugins-official"


def official_cfg(**kw) -> PluginsConfig:
    return PluginsConfig.model_validate({"install": [{"plugin": f"superpowers@{OFFICIAL}", **kw}]})


async def test_official_marketplace_unknown_falls_back_to_add(fake_claude_bin, tmp_path, log):
    report, _ = await go(official_cfg(), fake_claude_bin, tmp_path)
    assert report.installed == [f"superpowers@{OFFICIAL}"]
    assert argvs(log)[:3] == [
        ["plugin", "marketplace", "list", "--json"],
        ["plugin", "marketplace", "update", OFFICIAL],
        [
            "plugin",
            "marketplace",
            "add",
            "anthropics/claude-plugins-official",
            "--scope",
            "user",
            "--json",
        ],
    ]


async def test_official_marketplace_known_is_updated_not_added(fake_claude_bin, tmp_path, log):
    await go(official_cfg(), fake_claude_bin, tmp_path)
    n = len(entries(log))
    report, _ = await go(official_cfg(), fake_claude_bin, tmp_path)
    assert report.already_present == [f"superpowers@{OFFICIAL}"]
    assert argvs(log)[n : n + 2] == [
        ["plugin", "marketplace", "list", "--json"],
        ["plugin", "marketplace", "update", OFFICIAL],
    ]
    assert not any(a[:3] == ["plugin", "marketplace", "add"] for a in argvs(log)[n:])


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


def _stub(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


# plugin list never contains the installed plugin; install itself succeeds
_OMITTING_STUB = """\
case "$*" in
  "plugin marketplace list --json") echo '[{"name":"wb"}]' ;;
  "plugin list --json") echo '[]' ;;
  *) echo '{}' ;;
esac
"""


async def test_post_install_check_required_missing_raises_optional_warns(tmp_path):
    stub = _stub(tmp_path / "claude", _OMITTING_STUB)
    mk = marketplace(tmp_path)
    with pytest.raises(PluginInstallError) as exc:
        await go(cfg(str(mk)), stub, tmp_path)
    assert exc.value.plugin_id == ID
    assert exc.value.exit_code == 0

    report, _ = await go(cfg(str(mk), required=False), stub, tmp_path)
    assert report.installed == []
    assert len(report.warnings) == 1 and ID in report.warnings[0]


@pytest.fixture
def live_stdin():
    """Point fd 0 at a pipe that holds data and stays open, so an inherited stdin is observable."""
    r, w = os.pipe()
    os.write(w, b"data\n" * 50)
    saved = os.dup(0)
    os.dup2(r, 0)
    try:
        yield
    finally:
        os.dup2(saved, 0)
        for fd in (saved, r, w):
            os.close(fd)


async def test_subprocess_stdin_is_closed(tmp_path, monkeypatch, live_stdin):
    seen = tmp_path / "stdin.log"
    stub = _stub(
        tmp_path / "claude",
        f'if read -r _l; then echo open >> "{seen}"; else echo eof >> "{seen}"; fi\n'
        + _OMITTING_STUB,
    )
    with pytest.raises(PluginInstallError):
        await go(cfg(str(marketplace(tmp_path))), stub, tmp_path)
    lines = seen.read_text().split()
    assert len(lines) >= 3 and set(lines) == {"eof"}

    # the mise subprocesses are spawned the same way
    from claude_code_wrapper.config import MiseConfig
    from claude_code_wrapper.tooling import install_global, render_global_mise

    mseen = tmp_path / "mise.log"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    probe = f'if read -r _l; then echo open >> "{mseen}"; else echo eof >> "{mseen}"; fi\n'
    _stub(bindir / "mise", probe)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    mcfg = MiseConfig(tools={"node": "22"}, global_config_path=str(tmp_path / "g.toml"))
    render_global_mise(mcfg)
    await install_global(mcfg, {"PATH": os.environ["PATH"]})
    assert mseen.read_text().split() == ["eof"]


async def test_marketplace_update_failure_is_a_warning_and_install_continues(
    fake_claude_bin, tmp_path, log
):
    c = cfg(str(marketplace(tmp_path)))
    report, _ = await go(c, fake_claude_bin, tmp_path, {"FAKE_CLAUDE_UPDATE_FAIL": "1"})
    assert report.installed == [ID]
    assert len(report.warnings) == 1 and "wb" in report.warnings[0]
    assert ["plugin", "install", ID, "-s", "user", "-y", "--json"] in argvs(log)


async def test_install_failure_surfaces_after_a_failed_marketplace_update(
    fake_claude_bin, tmp_path, log
):
    c = cfg(str(marketplace(tmp_path)))
    env = {"FAKE_CLAUDE_UPDATE_FAIL": "1", "FAKE_CLAUDE_PLUGIN_FAIL": ID}
    with pytest.raises(PluginInstallError) as ei:
        await go(c, fake_claude_bin, tmp_path, env)
    assert ei.value.plugin_id == ID
