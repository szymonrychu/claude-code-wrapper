from __future__ import annotations

import stat
import tomllib
from pathlib import Path

import pytest

from claude_code_wrapper.config import MiseConfig
from claude_code_wrapper.tooling import (
    ToolingError,
    install_global,
    install_workdir,
    mise_env,
    render_global_mise,
)


@pytest.fixture
def fake_mise(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "mise.log"
    m = bindir / "mise"
    m.write_text(
        '#!/bin/sh\necho "$PWD|$MISE_GLOBAL_CONFIG_FILE|$MISE_YES|$*" >> '
        f'"{log}"\n[ -n "$MISE_FAIL" ] && exit 4\nexit 0\n'
    )
    m.chmod(m.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    return log


def cfg(tmp_path: Path, **kw) -> MiseConfig:
    return MiseConfig.model_validate({"global_config_path": str(tmp_path / "g/config.toml"), **kw})


def test_render_global_toml(tmp_path):
    c = cfg(tmp_path, tools={"node": "22", "python": "3.12"})
    p = render_global_mise(c)
    assert p == tmp_path / "g/config.toml"
    assert tomllib.loads(p.read_text()) == {"tools": {"node": "22", "python": "3.12"}}


def test_render_config_file_wins(tmp_path):
    src = tmp_path / "src.toml"
    src.write_text('[tools]\ngo = "1.22"\n')
    c = cfg(tmp_path, config_file=str(src), tools={"node": "22"})
    p = render_global_mise(c)
    assert p.read_text() == src.read_text()


def test_render_none(tmp_path):
    assert render_global_mise(cfg(tmp_path)) is None


async def test_install_global_invokes_mise_install(tmp_path, fake_mise):
    c = cfg(tmp_path, tools={"node": "22"})
    render_global_mise(c)
    await install_global(c, {})
    line = fake_mise.read_text().strip()
    assert line.endswith("|install") and str(tmp_path / "g/config.toml") in line and "|1|" in line


async def test_install_global_skips_when_not_rendered(tmp_path, fake_mise):
    await install_global(cfg(tmp_path), {})
    assert not fake_mise.exists()


async def test_failure_required_raises(tmp_path, fake_mise, caplog):
    c = cfg(tmp_path, tools={"node": "22"})
    render_global_mise(c)
    with pytest.raises(ToolingError):
        await install_global(c, {"MISE_FAIL": "1"})
    c2 = cfg(tmp_path, tools={"node": "22"}, required=False)
    render_global_mise(c2)
    await install_global(c2, {"MISE_FAIL": "1"})


async def test_missing_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    c = cfg(tmp_path, tools={"node": "22"})
    render_global_mise(c)
    with pytest.raises(ToolingError):
        await install_global(c, {})


async def test_install_workdir_trusts_then_installs(tmp_path, fake_mise):
    wd = tmp_path / "wd"
    wd.mkdir()
    assert await install_workdir(cfg(tmp_path), wd, {}) is False
    (wd / ".mise.toml").write_text("")
    assert await install_workdir(cfg(tmp_path), wd, {}) is True
    lines = fake_mise.read_text().splitlines()
    assert lines[0].endswith(f"|trust {wd / '.mise.toml'}")
    assert lines[1].endswith("|install") and lines[1].startswith(str(wd.resolve()))
    assert await install_workdir(cfg(tmp_path, install_workdir=False), wd, {}) is False


def test_mise_env_vars(tmp_path, monkeypatch):
    monkeypatch.setenv("MISE_DATA_DIR", "/data/mise")
    c = cfg(tmp_path, tools={"node": "22"}, trusted_paths=["/workdir", "/x"])
    render_global_mise(c)
    env = mise_env(c)
    assert env == {
        "MISE_GLOBAL_CONFIG_FILE": str(tmp_path / "g/config.toml"),
        "MISE_TRUSTED_CONFIG_PATHS": "/workdir:/x",
        "MISE_YES": "1",
        "MISE_DATA_DIR": "/data/mise",
    }
    monkeypatch.delenv("MISE_DATA_DIR")
    assert "MISE_DATA_DIR" not in mise_env(cfg(tmp_path))
    assert "MISE_GLOBAL_CONFIG_FILE" not in mise_env(cfg(tmp_path))


async def test_install_workdir_noop_without_config(tmp_path, fake_mise):
    wd = tmp_path / "wd"
    wd.mkdir()
    assert await install_workdir(cfg(tmp_path), wd, {}) is False
    assert not fake_mise.exists()
