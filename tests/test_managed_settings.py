"""Managed-settings baseline: render (D1), install and fallback (D2, Q3)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
from pathlib import Path

import pytest

from claude_code_wrapper.bootstrap.settings import (
    MANAGED_RELAY_EVENTS,
    PERMISSION_HOOK_CEILING,
    relay_timeouts,
    render_managed_settings,
)
from claude_code_wrapper.config import Config, ConfigError, load_config

RELAY = "/x/ccw-hook"
MANAGED_DIR = Path("/etc/claude-code")


def _cfg(**kw):
    return Config.model_validate(kw)


def _expected(pretooluse_timeout: float) -> dict:
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{RELAY} PreToolUse",
                            "timeout": pretooluse_timeout,
                            "onFailure": "block",
                        }
                    ],
                }
            ],
            "PermissionRequest": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{RELAY} PermissionRequest",
                            "timeout": 86430,
                            "onFailure": "block",
                        }
                    ],
                }
            ],
        },
        "pluginConfigs": {
            "cc-plugin-sec-default@builtin": {"options": {"allowManagedModsOnly": True}}
        },
        "disableAgentView": True,
        "permissions": {"deny": ["Edit(//etc/claude-code/**)", "Write(//etc/claude-code/**)"]},
        "env": {"CLAUDE_CODE_EXPERIMENTAL_CODE_MODE": "0", "CLAUDE_CODE_DISABLE_AGENT_VIEW": "1"},
    }


def test_render_managed_settings_exact():
    cfg = Config()
    doc = render_managed_settings(cfg, RELAY, MANAGED_DIR)
    assert doc == _expected(relay_timeouts(cfg).hook_timeout)
    assert MANAGED_RELAY_EVENTS == ("PreToolUse", "PermissionRequest")
    assert PERMISSION_HOOK_CEILING == 86430


def test_render_managed_pretooluse_timeout_follows_blocking_hooks():
    cfg = _cfg(
        hooks={
            "onAgentToolCall": [
                {"type": "command", "run": "x", "blocking": True, "timeout_seconds": 100}
            ]
        }
    )
    doc = render_managed_settings(cfg, RELAY, MANAGED_DIR)
    assert doc["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] == 105
    assert doc["hooks"]["PermissionRequest"][0]["hooks"][0]["timeout"] == 86430


def test_render_managed_never_sets_hook_kill_switches():
    """allowManagedHooksOnly skips the statusLine relay and the 9 settings.json relay events
    (N-249, F1.10); disableAllHooks turns the managed relay off (F1.11)."""
    doc = render_managed_settings(Config(), RELAY, MANAGED_DIR)
    assert "allowManagedHooksOnly" not in doc
    assert "disableAllHooks" not in doc
    assert "statusLine" not in doc


# ---------------------------------------------------------------- install (D2, Q3)


def _baseline() -> dict:
    return render_managed_settings(Config(), RELAY, MANAGED_DIR)


def _managed(tmp_path: Path, baseline: dict | None = None):
    from claude_code_wrapper.managed_settings import ManagedSettings

    d = tmp_path / "claude-code"
    d.mkdir(exist_ok=True)
    return ManagedSettings(d / "managed-settings.json", baseline or _baseline(), RELAY)


def test_install_writes_0644_active(tmp_path):
    ms = _managed(tmp_path)
    status = ms.install()
    assert status.state == "active"
    assert ms.active
    raw = ms.path.read_bytes()
    assert stat.S_IMODE(ms.path.stat().st_mode) == 0o644
    assert json.loads(raw) == _baseline()
    assert ms.document == _baseline()
    assert ms.status().sha256 == hashlib.sha256(raw).hexdigest()
    assert raw == json.dumps(_baseline(), indent=2, sort_keys=True).encode() + b"\n"
    d = ms.status().to_dict()
    assert d == {
        "state": "active",
        "path": str(ms.path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "reason": None,
        "restores": 0,
        "overlay": {},
    }
    assert sorted(os.listdir(ms.path.parent)) == ["managed-settings.json"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a 0555 dir")
def test_install_unwritable_dir_falls_back(tmp_path, caplog):
    ms = _managed(tmp_path)
    ms.path.parent.chmod(0o555)
    try:
        with caplog.at_level(logging.WARNING):
            status = ms.install()
    finally:
        ms.path.parent.chmod(0o755)
    assert status.state == "fallback"
    assert not ms.active
    assert status.reason
    assert status.sha256 is None
    assert not ms.path.exists()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings
    text = " ".join(r.getMessage() for r in warnings)
    assert str(ms.path) in text
    assert "PreToolUse" not in text  # never the content


def test_install_readback_without_relay_falls_back(tmp_path, caplog):
    baseline = _baseline()
    baseline["hooks"] = {}
    ms = _managed(tmp_path, baseline)
    with caplog.at_level(logging.WARNING):
        status = ms.install()
    assert status.state == "fallback"
    assert "relay" in (status.reason or "")
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_install_replaces_symlink(tmp_path):
    ms = _managed(tmp_path)
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    ms.path.symlink_to(target)
    assert ms.install().state == "active"
    assert ms.path.is_file() and not ms.path.is_symlink()
    assert target.read_text() == "{}"


def test_install_refuses_shared_dir(tmp_path, caplog):
    """R1: the managed dir must hold nothing but the file (and claude's own managed names)."""
    ms = _managed(tmp_path)
    (ms.path.parent / "unrelated.txt").write_text("x")
    with caplog.at_level(logging.WARNING):
        status = ms.install()
    assert status.state == "fallback"
    assert "not dedicated" in (status.reason or "")
    assert not ms.path.exists()
    assert (ms.path.parent / "unrelated.txt").exists()


def test_install_allows_claude_managed_names(tmp_path):
    ms = _managed(tmp_path)
    (ms.path.parent / "managed-settings.d").mkdir()
    (ms.path.parent / "managed-mcp.json").write_text("{}")
    (ms.path.parent / ".managed-settings.json.ccw-tmp").write_text("stale")
    assert ms.install().state == "active"


def test_status_before_install_is_fallback(tmp_path):
    ms = _managed(tmp_path)
    assert ms.status().state == "fallback"
    assert not ms.active


def test_has_relay():
    from claude_code_wrapper.managed_settings import has_relay

    assert has_relay(_baseline(), RELAY)
    doc = _baseline()
    doc["hooks"].pop("PermissionRequest")
    assert not has_relay(doc, RELAY)
    assert not has_relay([], RELAY)
    assert not has_relay({"hooks": {"PreToolUse": "x", "PermissionRequest": [1]}}, RELAY)


def test_write_managed_mode_and_no_temp_left(tmp_path):
    from claude_code_wrapper.managed_settings import TMP_NAME, write_managed

    path = tmp_path / "managed-settings.json"
    write_managed(path, b"{}\n")
    assert path.read_bytes() == b"{}\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert not (tmp_path / TMP_NAME).exists()


def test_paths_managed_settings_must_be_absolute(tmp_path):
    assert Config().paths.managed_settings == "/etc/claude-code/managed-settings.json"
    with pytest.raises(ValueError):
        _cfg(paths={"managed_settings": "etc/claude-code/managed-settings.json"})
    ok = _cfg(paths={"managed_settings": str(tmp_path / "m" / "managed-settings.json")})
    assert ok.paths.managed_settings.endswith("/m/managed-settings.json")


@pytest.mark.parametrize("parent", ["/", "/etc", "/tmp"])
def test_paths_managed_settings_rejects_shared_parent(parent):
    """R1: the parent dir is the managed dir; a system-wide dir cannot be dedicated."""
    with pytest.raises(ValueError, match="dedicated"):
        _cfg(paths={"managed_settings": str(Path(parent) / "managed-settings.json")})


def test_example_config_carries_managed_settings_path():
    from importlib import resources

    text = resources.files("claude_code_wrapper.data").joinpath("config.example.yaml")
    assert "managed_settings: /etc/claude-code/managed-settings.json" in text.read_text()
    try:
        load_config(str(text))
    except ConfigError as exc:  # pragma: no cover - the example must validate
        pytest.fail(str(exc))


# ---------------------------------------------------------------- wiring (D3)


def _session_hooks(world, sid: str) -> dict:
    doc = json.loads((world.session_dir(sid) / "config" / "settings.json").read_text())
    return doc["hooks"]


def _relayed(hooks: dict) -> set[str]:
    return {
        event
        for event, groups in hooks.items()
        if any("ccw-hook" in h.get("command", "") for g in groups for h in g.get("hooks", []))
    }


async def test_fallback_session_keeps_full_relay(world, tmp_path):
    from claude_code_wrapper.hook_relay import CLAUDE_HOOK_EVENTS
    from claude_code_wrapper.managed_settings import ManagedSettings

    cfg = world.cfg()
    path = tmp_path / "missing-dir" / "managed-settings.json"
    ms = ManagedSettings(path, _baseline(), RELAY)
    assert ms.install().state == "fallback"
    m = world.manager(cfg, relay_command=RELAY, managed_settings=ms)
    assert m.managed_settings is ms
    snap = await m.create({})
    assert _relayed(_session_hooks(world, snap["session_id"])) == set(CLAUDE_HOOK_EVENTS)


async def test_active_session_omits_managed_events(world, tmp_path):
    from claude_code_wrapper.hook_relay import CLAUDE_HOOK_EVENTS
    from claude_code_wrapper.managed_settings import ManagedSettings

    cfg = world.cfg()
    (tmp_path / "cc").mkdir()
    ms = ManagedSettings(tmp_path / "cc" / "managed-settings.json", _baseline(), RELAY)
    assert ms.install().state == "active"
    m = world.manager(cfg, relay_command=RELAY, managed_settings=ms)
    snap = await m.create({})
    relayed = _relayed(_session_hooks(world, snap["session_id"]))
    assert relayed == set(CLAUDE_HOOK_EVENTS) - set(MANAGED_RELAY_EVENTS)
    assert len(relayed) == 9


async def test_statusline_relay_works_with_managed_file_active(world):
    """N-249: claude skips statusLine under allowManagedHooksOnly; the managed baseline never
    sets it, so the settings.json statusLine relay still reaches the session."""
    payload = {"context_window": {"used_percentage": 42.0, "context_window_size": 200000}}
    cfg = world.cfg({"turns": [{"match": "status", "steps": [{"statusline": payload}]}]})
    m = world.manager(cfg)
    assert m.managed_settings is not None and m.managed_settings.active
    snap = await m.create({})
    sid = snap["session_id"]
    await m.router(sid).submit_turn("status")
    await world.wait_turn(m, sid)
    await world.until(lambda: (m.router(sid).statusline or {}).get("used_percentage") == 42.0)
