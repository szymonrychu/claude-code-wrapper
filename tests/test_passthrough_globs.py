from __future__ import annotations

from claude_code_wrapper.config import ClaudeConfig
from claude_code_wrapper.session import passthrough_names

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
