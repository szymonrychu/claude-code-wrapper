"""The peers token is a shared credential: config refuses a missing, short or reused one."""

from __future__ import annotations

import pytest

from claude_code_wrapper.config import ConfigError, load_config

pytestmark = pytest.mark.critical

TOKEN = "t" * 16


def load(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return load_config(p)


def err(tmp_path, text):
    with pytest.raises(ConfigError) as exc:
        load(tmp_path, text)
    return str(exc.value)


def enabled(token=TOKEN):
    return f"peers:\n  enabled: true\n  token: {token!r}\n"


def test_token_required_when_enabled(tmp_path):
    assert "peers.token: required when peers.enabled" in err(tmp_path, "peers:\n  enabled: true\n")


def test_token_too_short(tmp_path):
    assert "peers.token:" in err(tmp_path, enabled(token="short"))


def test_token_differs_from_auth_token(tmp_path):
    msg = err(tmp_path, f"server:\n  auth_token: {TOKEN}\n" + enabled())
    assert "peers.token: must differ from server.auth_token" in msg
