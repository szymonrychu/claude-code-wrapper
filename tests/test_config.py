import json
import os

import pytest

from claude_code_wrapper.config import (
    Config,
    ConfigError,
    load_config,
)


def write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


def err(tmp_path, text):
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, text))
    return str(exc.value)


def test_dangerous_flag_rejected(tmp_path):
    msg = err(tmp_path, "claude: {extra_args: ['--dangerously-skip-permissions']}\n")
    assert "claude.extra_args:" in msg


def test_redacted_masks_secrets(tmp_path, monkeypatch):
    cfg = load_config(
        write(
            tmp_path,
            """
server: {auth_token: tok-secret-1, callback_secret: cb-secret-1}
claude:
  env: {claude_code_oauth_token: sk-ant-oat-verysecret, extra: {MY_SECRET: hush}}
  mcp_servers:
    a: {type: stdio, command: foo, env: {API_TOKEN: envsecret, PLAIN: visible}}
    b: {type: http, url: 'https://h', headers: {Authorization: Bearer zzz}}
hooks:
  onTurn: [{type: webhook, url: 'https://x', secret: whsecret, headers: {X: y}}]
""",
        )
    )
    red = cfg.redacted()
    dumped = json.dumps(red)
    for s in (
        "tok-secret-1",
        "cb-secret-1",
        "verysecret",
        "hush",
        "envsecret",
        "Bearer zzz",
        "whsecret",
    ):
        assert s not in dumped
    assert red["server"]["auth_token"] == "***"
    assert red["claude"]["mcp_servers"]["a"]["env"]["PLAIN"] == "visible"
    assert red["claude"]["mcp_servers"]["b"]["headers"]["Authorization"] == "***"
    assert red["hooks"]["onTurn"][0]["secret"] == "***"
    assert Config().redacted()["server"]["auth_token"] == ""


# ---------------------------------------------------------------- usage forwarding

USAGE_TOKEN = "usage-ingest-token-0123456789"


def test_usage_token_redacted():
    cfg = Config.model_validate(
        {
            "usage": {
                "endpoint": "http://usage.test:8002/samples",
                "token": USAGE_TOKEN,
                "agent": "pod-0",
            }
        }
    )
    red = cfg.redacted()
    assert red["usage"]["token"] == "***"
    assert red["usage"]["endpoint"] == "http://usage.test:8002/samples"
    assert USAGE_TOKEN not in json.dumps(red)
    assert Config().redacted()["usage"]["token"] == ""


def test_claude_env_never_carries_usage_token(tmp_path, monkeypatch):
    from claude_code_wrapper.claude_env import ClaudeEnv
    from claude_code_wrapper.session import build_env

    monkeypatch.setenv("CCW_USAGE_TOKEN", USAGE_TOKEN)
    cfg = Config.model_validate(
        {
            "usage": {"endpoint": "http://usage.test:8002/samples", "token": USAGE_TOKEN},
            "claude": {"passthrough_env": ["*", "CCW_USAGE_TOKEN"]},
        }
    )
    env = build_env(
        service_env=dict(os.environ),
        claude_env=ClaudeEnv(),
        config_dir=str(tmp_path / "config"),
        gitconfig=str(tmp_path / "gitconfig"),
        session_id="s1",
        internal_url="http://127.0.0.1:8090",
        hook_token="hook-token-0123456789",
        gate_fail="allow",
        gate_timeout=14,
        permission_timeout=1815.0,
        mise_env=None,
        passthrough=cfg.claude.passthrough_env,
        passthrough_exclude=cfg.claude.passthrough_env_exclude,
    )
    assert "CCW_USAGE_TOKEN" not in env
    assert USAGE_TOKEN not in env.values()


def test_peers_token_redacted(tmp_path):
    token = "t" * 16
    cfg = load_config(write(tmp_path, f"peers:\n  enabled: true\n  token: {token!r}\n"))
    assert cfg.redacted()["peers"]["token"] == "***"
    assert token not in str(cfg.redacted())
    assert Config().redacted()["peers"]["token"] == ""
