"""The opt-in `peers:` config section (design D2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_code_wrapper.config import Config, ConfigError, PeersConfig, load_config

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "t" * 16


def load(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return load_config(p)


def err(tmp_path, text):
    with pytest.raises(ConfigError) as exc:
        load(tmp_path, text)
    return str(exc.value)


def enabled(extra="", token=TOKEN):
    return f"peers:\n  enabled: true\n  token: {token!r}\n{extra}"


def test_defaults():
    p = Config().peers
    assert p.model_dump() == {
        "enabled": False,
        "name": "",
        "advertise_url": "",
        "token": "",
        "static": [],
        "heartbeat_seconds": 10,
        "max_missed_heartbeats": 3,
        "join_timeout_seconds": 10,
        "max_message_bytes": 65536,
        "inbox_max": 32,
        "max_clock_skew_seconds": 60,
        "max_messages_per_minute": 30,
    }


def test_absent_section_and_disabled_need_nothing(tmp_path):
    assert load(tmp_path, "").peers.enabled is False
    assert load(tmp_path, "peers:\n  enabled: false\n").peers.token == ""


def test_token_required_when_enabled(tmp_path):
    msg = err(tmp_path, "peers:\n  enabled: true\n")
    assert (
        "peers.token: required when peers.enabled; set it, normally from ${CCW_PEER_TOKEN}" in msg
    )


def test_token_too_short(tmp_path):
    assert "peers.token:" in err(tmp_path, enabled(token="short"))


def test_token_differs_from_auth_token(tmp_path):
    msg = err(tmp_path, f"server:\n  auth_token: {TOKEN}\n" + enabled())
    assert "peers.token: must differ from server.auth_token" in msg
    load(tmp_path, "server:\n  allow_anonymous: true\n" + enabled())


@pytest.mark.parametrize("name", ["a b", "x" * 65, ".", "..", ".hidden", "a/b", "é"])
def test_bad_names(tmp_path, name):
    assert "peers.name:" in err(tmp_path, enabled(f"  name: {name!r}\n"))


@pytest.mark.parametrize("name", ["", "a", "node-1.cluster_a", "x" * 64, "a.."])
def test_good_names(tmp_path, name):
    assert load(tmp_path, enabled(f"  name: {name!r}\n")).peers.name == name


@pytest.mark.parametrize(
    "url", ["ftp://h", "h:80", "http://", "http://h/", "/x", "http://h/path/", "nonsense"]
)
def test_bad_advertise_url(tmp_path, url):
    assert "peers.advertise_url:" in err(tmp_path, enabled(f"  advertise_url: {url!r}\n"))


@pytest.mark.parametrize(
    "url", ["", "http://h:8080", "https://h.example/base", "http://h:{port}", "http://{port}.x"]
)
def test_good_advertise_url(tmp_path, url):
    assert load(tmp_path, enabled(f"  advertise_url: {url!r}\n")).peers.advertise_url == url


def test_static_string_and_url_items_dedup(tmp_path):
    cfg = load(
        tmp_path,
        enabled(
            "  static:\n    - http://a:1\n    - {url: 'http://b:2'}\n    - http://a:1\n"
            "    - https://b.example\n"
        ),
    )
    assert cfg.peers.static == ["http://a:1", "http://b:2", "https://b.example"]


def test_static_bad_item_path(tmp_path):
    msg = err(tmp_path, enabled("  static:\n    - http://a:1\n    - http://b/\n"))
    assert "peers.static.1:" in msg
    assert "peers.static.0:" in err(tmp_path, enabled("  static:\n    - 'ftp://x'\n"))


@pytest.mark.parametrize(
    "key,value",
    [
        ("heartbeat_seconds", 0),
        ("max_missed_heartbeats", 0),
        ("join_timeout_seconds", 0),
        ("max_message_bytes", 0),
        ("max_message_bytes", 1048577),
        ("inbox_max", 0),
        ("max_clock_skew_seconds", 0),
        ("max_messages_per_minute", 0),
    ],
)
def test_numeric_bounds(tmp_path, key, value):
    assert f"peers.{key}:" in err(tmp_path, enabled(f"  {key}: {value}\n"))


def test_numeric_edges_accepted(tmp_path):
    cfg = load(tmp_path, enabled("  max_message_bytes: 1048576\n  max_missed_heartbeats: 1\n"))
    assert cfg.peers.max_message_bytes == 1048576


def test_user_ccw_mcp_server_reserved_when_enabled(tmp_path):
    mcp = "claude:\n  mcp_servers:\n    ccw:\n      type: stdio\n      command: x\n"
    assert "claude.mcp_servers.ccw:" in err(tmp_path, mcp + enabled())
    load(tmp_path, mcp)


def test_with_static():
    base = PeersConfig(static=["http://a:1"])
    out = base.with_static(["http://b:2", "http://a:1"])
    assert out.static == ["http://a:1", "http://b:2"]
    assert base.static == ["http://a:1"]
    with pytest.raises(ValueError):
        base.with_static(["nope"])


def test_redacted_token(tmp_path):
    cfg = load(tmp_path, enabled())
    assert cfg.redacted()["peers"]["token"] == "***"
    assert TOKEN not in str(cfg.redacted())
    assert Config().redacted()["peers"]["token"] == ""


def test_example_config_loads_with_peers_disabled(monkeypatch):
    monkeypatch.delenv("CCW_PEER_TOKEN", raising=False)
    cfg = load_config(ROOT / "src/claude_code_wrapper/data/config.example.yaml")
    assert cfg.peers == PeersConfig()


def test_defaults_disabled(tmp_path):
    test_defaults()
    test_absent_section_and_disabled_need_nothing(tmp_path)


def test_token_rules(tmp_path):
    test_token_required_when_enabled(tmp_path)
    test_token_too_short(tmp_path)
    test_token_differs_from_auth_token(tmp_path)


def test_key_validation(tmp_path):
    for name in ["a b", "x" * 65, ".", "..", ".hidden", "a/b"]:
        test_bad_names(tmp_path, name)
    for url in ["ftp://h", "h:80", "http://", "http://h/"]:
        test_bad_advertise_url(tmp_path, url)
    test_good_advertise_url(tmp_path, "http://h:{port}")
    for key, value in [
        ("heartbeat_seconds", 0),
        ("max_missed_heartbeats", 0),
        ("join_timeout_seconds", 0),
        ("max_message_bytes", 0),
        ("max_message_bytes", 1048577),
        ("inbox_max", 0),
        ("max_clock_skew_seconds", 0),
        ("max_messages_per_minute", 0),
    ]:
        test_numeric_bounds(tmp_path, key, value)


def test_static_seeds(tmp_path):
    test_static_string_and_url_items_dedup(tmp_path)
    test_static_bad_item_path(tmp_path)
    test_with_static()


def test_ccw_name_reserved_with_peers(tmp_path):
    test_user_ccw_mcp_server_reserved_when_enabled(tmp_path)


def test_token_redacted(tmp_path):
    test_redacted_token(tmp_path)
