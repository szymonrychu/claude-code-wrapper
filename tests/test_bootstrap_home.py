import json
import os
import stat
from pathlib import Path

import pytest

from claude_code_wrapper.bootstrap import (
    MARKER,
    BootstrapError,
    copy_config_files,
    ensure_config_dir,
    seed_claude_json,
    write_instructions,
)
from claude_code_wrapper.config import ClaudeConfig, PromptsConfig


def _cfg(**kw):
    return ClaudeConfig.model_validate(kw)


def _read(cd: Path) -> dict:
    return json.loads((cd / ".claude.json").read_text())


def test_ensure_config_dir_mode(tmp_path):
    cd = tmp_path / "a" / "cfg"
    ensure_config_dir(cd)
    assert stat.S_IMODE(cd.stat().st_mode) == 0o700


def test_seed_basic(tmp_path):
    cd, wd = tmp_path / "cfg", tmp_path / "wd"
    wd.mkdir()
    ensure_config_dir(cd)
    c = _cfg()
    seed_claude_json(cd, wd, c, c.env)
    data = _read(cd)
    assert data["hasCompletedOnboarding"] is True
    assert data["autoUpdates"] is False
    assert data["bypassPermissionsModeAccepted"] is True
    assert data["projects"][os.path.realpath(wd)]["hasTrustDialogAccepted"] is True
    assert stat.S_IMODE((cd / ".claude.json").stat().st_mode) == 0o600
    assert [p.name for p in cd.iterdir()] == [".claude.json"]
    assert list(wd.iterdir()) == []


def test_no_bypass_flag_other_mode(tmp_path):
    c = _cfg(permission_mode="default")
    seed_claude_json(tmp_path, tmp_path, c, c.env)
    assert "bypassPermissionsModeAccepted" not in _read(tmp_path)


def test_symlinked_workdir_both_keys(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    cd = tmp_path / "cfg"
    c = _cfg()
    seed_claude_json(cd, link, c, c.env)
    projects = _read(cd)["projects"]
    assert set(projects) == {os.path.realpath(real), str(link)}


def test_merge_keeps_existing(tmp_path):
    (tmp_path / ".claude.json").write_text(
        json.dumps({"userID": "x", "projects": {"/other": {"foo": 1}}})
    )
    c = _cfg()
    seed_claude_json(tmp_path, tmp_path / "w", c, c.env)
    data = _read(tmp_path)
    assert data["userID"] == "x"
    assert data["projects"]["/other"] == {"foo": 1}


def test_api_key_approved_once(tmp_path):
    c = _cfg(env={"anthropic_api_key": "sk-ant-" + "a" * 30 + "TAIL0123456789012345"})
    key = c.env.anthropic_api_key.get_secret_value()
    (tmp_path / ".claude.json").write_text(
        json.dumps({"customApiKeyResponses": {"approved": [], "rejected": ["r"]}})
    )
    seed_claude_json(tmp_path, tmp_path, c, c.env)
    seed_claude_json(tmp_path, tmp_path, c, c.env)
    r = _read(tmp_path)["customApiKeyResponses"]
    assert r["approved"] == [key[-20:]]
    assert r["rejected"] == ["r"]


def test_no_api_key_no_responses(tmp_path):
    c = _cfg()
    seed_claude_json(tmp_path, tmp_path, c, c.env)
    assert "customApiKeyResponses" not in _read(tmp_path)


def test_mcp_servers_and_keep(tmp_path):
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {"old": {"type": "stdio"}}}))
    c = _cfg(
        mcp_servers={
            "s": {"type": "stdio", "command": "x", "args": ["a"], "env": {"K": "v"}},
            "h": {"type": "http", "url": "http://u", "headers": {"A": "b"}},
        }
    )
    seed_claude_json(tmp_path, tmp_path, c, c.env)
    m = _read(tmp_path)["mcpServers"]
    assert m["s"] == {"type": "stdio", "command": "x", "args": ["a"], "env": {"K": "v"}}
    assert m["h"] == {"type": "http", "url": "http://u", "headers": {"A": "b"}}
    assert m["old"] == {"type": "stdio"}


def test_claude_json_deep_merge_last(tmp_path):
    c = _cfg(claude_json={"autoUpdates": True, "projects": {"/p": {"x": 1}}, "l": [1]})
    seed_claude_json(tmp_path, tmp_path, c, c.env)
    data = _read(tmp_path)
    assert data["autoUpdates"] is True
    assert data["projects"]["/p"] == {"x": 1}
    assert os.path.realpath(tmp_path) in data["projects"]
    assert data["l"] == [1]


def test_instructions_written(tmp_path):
    f = tmp_path / "g.md"
    f.write_text("GLOBAL")
    write_instructions(tmp_path, PromptsConfig(global_claude_md=str(f), system_append="APPEND"))
    assert (tmp_path / "CLAUDE.md").read_text() == f"{MARKER}\nGLOBAL\n\nAPPEND"


def test_instructions_append_only(tmp_path):
    write_instructions(tmp_path, PromptsConfig(system_append="APPEND"))
    assert (tmp_path / "CLAUDE.md").read_text() == f"{MARKER}\nAPPEND"


def test_instructions_missing_file(tmp_path):
    with pytest.raises(BootstrapError):
        write_instructions(tmp_path, PromptsConfig(global_claude_md=str(tmp_path / "nope")))


def test_instructions_removal_rules(tmp_path):
    p = tmp_path / "CLAUDE.md"
    p.write_text(f"{MARKER}\nold")
    write_instructions(tmp_path, PromptsConfig())
    assert not p.exists()
    p.write_text("mine")
    write_instructions(tmp_path, PromptsConfig())
    assert p.read_text() == "mine"


def test_copy_files_and_dirs(tmp_path):
    src = tmp_path / "src"
    (src / "d" / "e").mkdir(parents=True)
    (src / "d" / "e" / "f.txt").write_text("1")
    (src / "one.txt").write_text("o")
    cd = tmp_path / "cfg"
    cd.mkdir()
    (cd / "dst").mkdir()
    (cd / "dst" / "old").write_text("x")
    c = _cfg()
    from claude_code_wrapper.config import ConfigFile

    c.config_files = [
        ConfigFile(source=str(src / "one.txt"), target="sub/one.txt"),
        ConfigFile(source=str(src / "d"), target="dst"),
    ]
    copy_config_files(cd, c)
    copy_config_files(cd, c)
    assert (cd / "sub" / "one.txt").read_text() == "o"
    assert (cd / "dst" / "e" / "f.txt").read_text() == "1"


def test_copy_errors(tmp_path):
    from claude_code_wrapper.config import ConfigFile

    cd = tmp_path / "cfg"
    cd.mkdir()
    c = _cfg()
    c.config_files = [ConfigFile(source=str(tmp_path / "missing"), target="a")]
    with pytest.raises(BootstrapError, match="missing"):
        copy_config_files(cd, c)
    (tmp_path / "s").write_text("1")
    (cd / "lnk").symlink_to(tmp_path)
    c.config_files = [ConfigFile(source=str(tmp_path / "s"), target="lnk/x")]
    with pytest.raises(BootstrapError):
        copy_config_files(cd, c)
    c.config_files = [ConfigFile.model_construct(source=str(tmp_path / "s"), target="/abs")]
    with pytest.raises(BootstrapError):
        copy_config_files(cd, c)
