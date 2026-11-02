from __future__ import annotations

import json
import os
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest

from claude_code_wrapper.claude_env import CREDENTIAL_ENV
from claude_code_wrapper.config import MiseConfig
from claude_code_wrapper.manager import CreateSessionRequest, SessionManager
from claude_code_wrapper.redact import Redactor
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


@pytest.fixture
def env_mise(tmp_path, monkeypatch):
    """A mise stand-in that records the environment it was started with."""
    bindir = tmp_path / "envbin"
    bindir.mkdir()
    dump = tmp_path / "mise.env"
    m = bindir / "mise"
    m.write_text(f'#!/bin/sh\nenv >> "{dump}"\nexit 0\n')
    m.chmod(m.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    return dump


USAGE_TOKEN = "usage-ingest-token-0123456789"


async def test_install_global_env_lacks_usage_token(tmp_path, env_mise, monkeypatch):
    monkeypatch.setenv("CCW_USAGE_TOKEN", USAGE_TOKEN)
    c = cfg(tmp_path, tools={"node": "22"})
    render_global_mise(c)
    await install_global(c, {"CCW_USAGE_TOKEN": USAGE_TOKEN, "KEEP_ME": "yes"})
    seen = env_mise.read_text()
    assert "KEEP_ME=yes" in seen
    assert "CCW_USAGE_TOKEN" not in seen and USAGE_TOKEN not in seen


async def test_install_workdir_env_lacks_usage_token(tmp_path, env_mise, monkeypatch):
    monkeypatch.setenv("CCW_USAGE_TOKEN", USAGE_TOKEN)
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "mise.toml").write_text("")
    assert await install_workdir(cfg(tmp_path), wd, {"CCW_USAGE_TOKEN": USAGE_TOKEN}) is True
    seen = env_mise.read_text()
    assert seen.count("MISE_YES=1") == 2  # trust and install both ran
    assert "CCW_USAGE_TOKEN" not in seen and USAGE_TOKEN not in seen


# The wrapper's own secrets never reach a mise child: mise runs repo-controlled config (a
# cloned repo's mise.toml defines tasks, hooks and env that execute code).
WRAPPER_SECRETS = {
    "CCW_AUTH_TOKEN": "wrapper-auth-sentinel-0123",
    "CCW_PEER_TOKEN": "wrapper-peer-sentinel-0123",
    "CCW_HOOK_TOKEN": "wrapper-hook-sentinel-0123",
    "CCW_USAGE_TOKEN": "wrapper-usage-sentinel-0123",
    "CLAUDE_CODE_OAUTH_TOKEN": "wrapper-oauth-sentinel-0123",
    "ANTHROPIC_API_KEY": "wrapper-apikey-sentinel-0123",
}


def assert_no_wrapper_secret(seen: str) -> None:
    for name, value in WRAPPER_SECRETS.items():
        assert f"{name}=" not in seen, name
        assert value not in seen, name


async def test_install_global_env_is_not_the_service_env(tmp_path, env_mise, monkeypatch):
    for name, value in WRAPPER_SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SERVICE_ONLY", "service-only-value")
    c = cfg(tmp_path, tools={"node": "22"})
    render_global_mise(c)
    await install_global(c, {"PATH": os.environ["PATH"], "KEEP_ME": "yes"})
    seen = env_mise.read_text()
    assert "KEEP_ME=yes" in seen
    assert "SERVICE_ONLY" not in seen
    assert_no_wrapper_secret(seen)


async def test_install_workdir_env_is_not_the_service_env(tmp_path, env_mise, monkeypatch):
    for name, value in WRAPPER_SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SERVICE_ONLY", "service-only-value")
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "mise.toml").write_text("")
    assert await install_workdir(cfg(tmp_path), wd, {"PATH": os.environ["PATH"]}) is True
    seen = env_mise.read_text()
    assert seen.count("MISE_YES=1") == 2
    assert "SERVICE_ONLY" not in seen
    assert_no_wrapper_secret(seen)


async def test_mise_drops_wrapper_secrets_handed_in(tmp_path, env_mise):
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "mise.toml").write_text("")
    c = cfg(tmp_path, tools={"node": "22"})
    render_global_mise(c)
    env = {"PATH": os.environ["PATH"], "GH_TOKEN": "operator-passthrough", **WRAPPER_SECRETS}
    await install_global(c, env)
    assert await install_workdir(c, wd, env) is True
    seen = env_mise.read_text()
    assert seen.count("GH_TOKEN=operator-passthrough") == 3  # install, trust, install
    assert_no_wrapper_secret(seen)


async def test_session_tool_children_get_no_wrapper_secret(world, tmp_path):
    """End to end: the setup command and the workdir mise install of a real session create
    get neither the service's secrets nor the claude credentials; claude still gets its token."""
    bindir = tmp_path / "misebin"
    bindir.mkdir()
    mise_dump = tmp_path / "mise.env"
    setup_dump = tmp_path / "setup.env"
    m_bin = bindir / "mise"
    m_bin.write_text(f'#!/bin/sh\nenv >> "{mise_dump}"\nexit 0\n')
    m_bin.chmod(m_bin.stat().st_mode | stat.S_IXUSR)
    service_env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        **WRAPPER_SECRETS,
        "SERVICE_ONLY": "service-only-value",
        "GH_TOKEN": "operator-passthrough",
    }
    oauth = "claude-oauth-sentinel-0123"
    c = world.cfg(
        claude={
            "env": {
                "claude_code_oauth_token": oauth,
                "extra": {"PLAIN_V": "plain"},
            },
            "passthrough_env": ["GH_TOKEN"],
        },
        init={"setup_commands": [f'env > "{setup_dump}"; : > mise.toml']},
    )
    m = SessionManager(c, internal_url=world.url, service_env=service_env, redactor=Redactor())
    world.managers.append(m)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    ctx = m._active[sid]
    for dump in (setup_dump, mise_dump):
        seen = dump.read_text()
        assert "GH_TOKEN=operator-passthrough" in seen and "PLAIN_V=plain" in seen
        assert "SERVICE_ONLY" not in seen
        assert_no_wrapper_secret(seen)
        for value in (oauth, ctx.hook_token):
            assert value not in seen
    assert m._child_env(ctx)["CLAUDE_CODE_OAUTH_TOKEN"] == oauth


def _dumping(bindir: Path, name: str, dump: Path, target: str, when: str = "true") -> Path:
    """An executable `name` in `bindir` that appends its env to `dump` (when the shell test
    `when` holds), then runs `target`."""
    exe = bindir / name
    exe.write_text(f'#!/bin/sh\nif {when}; then env >> "{dump}"; fi\nexec {target} "$@"\n')
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


@pytest.fixture
async def passthrough_all_session(world, tmp_path):
    """A clone-mode session with a setup command, a workdir mise.toml and a plugin install,
    under `claude.passthrough_env: ["*"]` and a service env holding every wrapper secret.
    Each tool child dumps its env to its own file. Returns (manager, ctx, dumps)."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    origin = tmp_path / "origin"
    origin.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "a"],
    ):
        subprocess.run(["git", *args], cwd=origin, env=env, check=True)
    bindir = tmp_path / "dumpbin"
    bindir.mkdir()
    dumps = {k: tmp_path / f"{k}.env" for k in ("git", "setup", "mise", "plugin")}
    _dumping(bindir, "git", dumps["git"], "/usr/bin/git")
    _dumping(bindir, "mise", dumps["mise"], "true")
    # the same binary runs the claude child, which may hold the OAuth token: dump plugin runs only
    claude = _dumping(
        bindir, "claude-dump", dumps["plugin"], str(world.bin), when='[ "$1" = plugin ]'
    )
    source = tmp_path / "mkt"
    (source / ".claude-plugin").mkdir(parents=True)
    (source / ".claude-plugin" / "marketplace.json").write_text(json.dumps({"name": "wb"}))
    service_env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        **WRAPPER_SECRETS,
        "GH_TOKEN": "operator-passthrough",
    }
    c = world.cfg(
        claude={
            "binary": str(claude),
            "env": {"claude_code_oauth_token": "claude-oauth-sentinel-0123"},
            "passthrough_env": ["*"],
        },
        init={
            "workdir": {"mode": "clone", "repo_url": f"file://{origin}", "name": "proj"},
            "setup_commands": [f'env > "{dumps["setup"]}"; : > mise.toml'],
        },
        plugins={
            "marketplaces": [{"name": "wb", "source": str(source)}],
            "install": [{"plugin": "workbench@wb"}],
        },
    )
    m = SessionManager(c, internal_url=world.url, service_env=service_env, redactor=Redactor())
    world.managers.append(m)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    return m, m._active[sid], dumps


@pytest.mark.parametrize("child", ["git", "setup", "mise", "plugin"])
async def test_passthrough_all_never_hands_a_tool_child_a_secret(passthrough_all_session, child):
    """`passthrough_env: ["*"]` selects every service name, Claude credentials included; still
    no repo-controlled child (git, setup commands, workdir mise, plugin installs) gets the OAuth
    token, the API key or a wrapper token. Only the claude child holds the OAuth token."""
    m, ctx, dumps = passthrough_all_session
    seen = dumps[child].read_text()
    assert "GH_TOKEN=operator-passthrough" in seen
    assert_no_wrapper_secret(seen)
    for value in ("claude-oauth-sentinel-0123", ctx.hook_token):
        assert value not in seen
    assert m._child_env(ctx)["CLAUDE_CODE_OAUTH_TOKEN"] == "claude-oauth-sentinel-0123"


async def test_tool_env_has_no_secret_under_passthrough_all(passthrough_all_session):
    m, ctx, _ = passthrough_all_session
    for gitconfig in (None, ctx.session_dir / "gitconfig"):
        env = m._tool_env(ctx, gitconfig)
        assert env["GH_TOKEN"] == "operator-passthrough"
        assert not [k for k in env if k.startswith("CCW_") or k in CREDENTIAL_ENV]
        assert "claude-oauth-sentinel-0123" not in env.values()
