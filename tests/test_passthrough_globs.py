from __future__ import annotations

from types import SimpleNamespace

from claude_code_wrapper.claude_env import ClaudeEnv
from claude_code_wrapper.config import ClaudeConfig, Config
from claude_code_wrapper.manager import SessionManager
from claude_code_wrapper.session import build_env, passthrough_names

SERVICE = {
    "GH_TOKEN": "a",
    "GH_HOST": "b",
    "WB_ONE": "1",
    "WB_SECRET_KEY": "s",
    "CCW_AUTH_TOKEN": "x",
    "CLAUDE_CONFIG_DIR": "/elsewhere",
    "OTHER": "o",
}


def test_plain_name_matches_only_itself():
    assert passthrough_names(SERVICE, ["GH_TOKEN"], []) == ["GH_TOKEN"]


def test_glob_with_exclude():
    got = passthrough_names(SERVICE, ["WB_*", "GH_*"], ["*SECRET*", "GH_HOST"])
    assert got == ["GH_TOKEN", "WB_ONE"]


def test_star_never_passes_ccw_or_managed():
    got = passthrough_names(SERVICE, ["*"], [])
    assert "CCW_AUTH_TOKEN" not in got and "CLAUDE_CONFIG_DIR" not in got
    assert {"GH_TOKEN", "OTHER"} <= set(got)


def test_case_sensitive():
    assert passthrough_names(SERVICE, ["gh_*"], []) == []


def test_config_field_defaults_and_parses():
    assert ClaudeConfig().passthrough_env_exclude == []
    assert ClaudeConfig(passthrough_env_exclude=["*KEY"]).passthrough_env_exclude == ["*KEY"]


def test_glob_matches_names():
    assert passthrough_names(SERVICE, ["WB_*"], []) == ["WB_ONE", "WB_SECRET_KEY"]


def test_exclude_removes_matches():
    assert passthrough_names(SERVICE, ["WB_*"], ["*SECRET*"]) == ["WB_ONE"]


def test_ccw_and_managed_never_pass_star():
    got = passthrough_names(SERVICE, ["*"], [])
    assert not any(n.startswith("CCW_") for n in got)
    assert "CLAUDE_CONFIG_DIR" not in got


def test_plain_names_unchanged():
    assert passthrough_names(SERVICE, ["GH_TOKEN", "NOPE"], []) == ["GH_TOKEN"]


PATTERNS = ["WB_*", "GH_*"]
EXCLUDES = ["*SECRET*"]


def test_tool_env_uses_same_rule():
    cfg = Config()
    cfg.claude.passthrough_env = PATTERNS
    cfg.claude.passthrough_env_exclude = EXCLUDES
    fake = SimpleNamespace(service_env=SERVICE, cfg=cfg)
    ctx = SimpleNamespace(claude_env=ClaudeEnv(), config_dir="/cfg")
    tool = SessionManager._tool_env(fake, ctx, None)
    child = build_env(
        service_env=SERVICE,
        claude_env=ClaudeEnv(),
        config_dir="/cfg",
        gitconfig="/g",
        session_id="s",
        internal_url="http://x",
        hook_token="t",
        gate_fail="allow",
        gate_timeout=1.0,
        mise_env=None,
        passthrough=PATTERNS,
        passthrough_exclude=EXCLUDES,
    )
    expected = set(passthrough_names(SERVICE, PATTERNS, EXCLUDES))
    assert expected == {"GH_TOKEN", "GH_HOST", "WB_ONE"}
    assert expected <= set(tool) and expected <= set(child)
    assert "WB_SECRET_KEY" not in tool and "WB_SECRET_KEY" not in child
