"""Peer session bootstrap: env, settings and CLAUDE.md per session (design D3, D4)."""

import json

from claude_code_wrapper.bootstrap.instructions import MARKER, write_instructions
from claude_code_wrapper.bootstrap.settings import PEERS_ALLOW, render_settings
from claude_code_wrapper.claude_env import ClaudeEnv
from claude_code_wrapper.config import Config, PromptsConfig
from claude_code_wrapper.session import build_env

RELAY = "/opt/ccw-hook"


def _env(**kw):
    base = {
        "service_env": {"PATH": "/usr/bin", "CLAUDE_CODE_HARBOR_KITE": "1"},
        "claude_env": ClaudeEnv(extra={"CLAUDE_CODE_HARBOR_KITE": "1"}),
        "config_dir": "/s/config",
        "gitconfig": "/s/gitconfig",
        "session_id": "ses_1",
        "internal_url": "http://127.0.0.1:8090",
        "hook_token": "tok",
        "gate_fail": "deny",
        "gate_timeout": 14.0,
        "mise_env": {},
        "passthrough": ["CLAUDE_CODE_HARBOR_KITE"],
    }
    base.update(kw)
    return build_env(**base)


def test_env_peers_forces_harbor_kite_off_over_user_value():
    assert _env(peers=True)["CLAUDE_CODE_HARBOR_KITE"] == "0"


def test_env_without_peers_is_unchanged():
    assert _env() == _env(peers=False)
    assert _env()["CLAUDE_CODE_HARBOR_KITE"] != "0"


def _settings(tmp_path, cfg=None, **kw):
    return render_settings(tmp_path, cfg or Config(), relay_command=RELAY, **kw)


def test_settings_peers_deny_and_allow(tmp_path):
    doc = _settings(tmp_path, peers=True)
    perms = doc["permissions"]
    assert {"SendMessage", "ListAgents"} <= set(perms["deny"])
    assert set(PEERS_ALLOW) == {"mcp__ccw__peers", "mcp__ccw__send_message"}
    assert set(PEERS_ALLOW) <= set(perms["allow"])


def test_settings_peers_no_duplicates(tmp_path):
    cfg = Config.model_validate(
        {
            "claude": {
                "permissions": {
                    "allow": ["mcp__ccw__peers"],
                    "deny": ["SendMessage"],
                }
            }
        }
    )
    perms = _settings(tmp_path, cfg, peers=True)["permissions"]
    assert perms["deny"].count("SendMessage") == 1
    assert perms["deny"].count("ListAgents") == 1
    assert perms["allow"].count("mcp__ccw__peers") == 1
    assert perms["allow"].count("mcp__ccw__send_message") == 1


def test_settings_rerender_without_peers_removes_ccw_allow(tmp_path):
    _settings(tmp_path, peers=True)
    perms = _settings(tmp_path)["permissions"]
    assert not set(PEERS_ALLOW) & set(perms["allow"])
    assert "SendMessage" not in perms["deny"]


def test_settings_disabled_is_byte_identical(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    render_settings(a, Config(), relay_command=RELAY)
    render_settings(b, Config(), relay_command=RELAY, peers=False)
    assert (a / "settings.json").read_bytes() == (b / "settings.json").read_bytes()
    assert "SendMessage" not in json.loads((a / "settings.json").read_text())["permissions"]["deny"]


def _prompts(**kw):
    return PromptsConfig.model_validate(kw)


def test_instructions_peers_alone_writes_claude_md(tmp_path):
    target = write_instructions(tmp_path, _prompts(), peers=True)
    text = target.read_text()
    assert text.startswith(MARKER)
    for needle in (
        "mcp__ccw__peers",
        "mcp__ccw__send_message",
        "<instance>/<session id>",
        "<agent-message from=",
        "msg_id=",
        "notify_when_idle",
        "<peer-idle-notice>",
        "<peer-delivery-failed>",
        "SendMessage",
        "ListAgents",
    ):
        assert needle in text


def test_instructions_peers_after_configured_parts(tmp_path):
    target = write_instructions(tmp_path, _prompts(system_append="BASE RULES"), peers=True)
    text = target.read_text()
    assert text.index("BASE RULES") < text.index("mcp__ccw__peers")


def test_instructions_without_peers_unchanged(tmp_path):
    assert write_instructions(tmp_path, _prompts()) is None
    assert not (tmp_path / "CLAUDE.md").exists()
    target = write_instructions(tmp_path, _prompts(system_append="BASE RULES"))
    assert target.read_text() == MARKER + "\nBASE RULES"
