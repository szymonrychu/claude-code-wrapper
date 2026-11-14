"""Tamper check of the managed-settings dir: drift kinds, restore, event fan-out (spec D4)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
from pathlib import Path

import httpx
import pytest

from claude_code_wrapper import managed_settings as ms_mod
from claude_code_wrapper.bootstrap.settings import MANAGED_RELAY_EVENTS, render_managed_settings
from claude_code_wrapper.config import Config
from claude_code_wrapper.hook_relay import CLAUDE_HOOK_EVENTS
from claude_code_wrapper.internal_api import HEADER_SESSION, HEADER_TOKEN, create_internal_app
from claude_code_wrapper.managed_settings import TMP_NAME, Drift, ManagedSettings

RELAY = "/x/ccw-hook"
GROUP = {"matcher": "Bash", "hooks": [{"type": "command", "command": "/bin/true"}]}


def _ms(tmp_path: Path, install: bool = True) -> ManagedSettings:
    d = tmp_path / "claude-code"
    d.mkdir(exist_ok=True)
    ms = ManagedSettings(
        d / "managed-settings.json",
        render_managed_settings(Config(), RELAY, d),
        RELAY,
    )
    if install:
        ms.install()
    return ms


@pytest.fixture
def ms(tmp_path):
    return _ms(tmp_path)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tamper_missing(ms):
    ms.path.unlink()


def _tamper_modified(ms):
    ms.path.write_text("{}")


def _tamper_mode(ms):
    ms.path.chmod(0o666)


def _tamper_symlink(ms):
    other = ms.path.parent.parent / "elsewhere.json"
    other.write_bytes(ms.path.read_bytes())
    ms.path.unlink()
    ms.path.symlink_to(other)


def _tamper_dropin(ms):
    d = ms.path.parent / "managed-settings.d"
    d.mkdir()
    (d / "10-x.json").write_text("{}")


def _tamper_mcp(ms):
    (ms.path.parent / "managed-mcp.json").write_text("{}")


TAMPERS = [
    ("missing", _tamper_missing),
    ("modified", _tamper_modified),
    ("mode", _tamper_mode),
    ("not_regular", _tamper_symlink),
    ("extra_entry", _tamper_dropin),
    ("extra_entry", _tamper_mcp),
]


def test_check_clean_returns_none(ms):
    assert ms.check("relay") is None
    assert ms.status().restores == 0


@pytest.mark.parametrize(("kind", "tamper"), TAMPERS)
def test_check_restores_drift(ms, kind, tamper):
    expected = ms.path.read_bytes()
    tamper(ms)
    drift = ms.check("turn")
    assert drift is not None
    assert (drift.kind, drift.restored, drift.trigger) == (kind, True, "turn")
    assert drift.expected_sha256 == _sha(expected)
    assert ms.path.read_bytes() == expected
    st = os.lstat(ms.path)
    assert stat.S_ISREG(st.st_mode)
    assert stat.S_IMODE(st.st_mode) == 0o644
    assert sorted(os.listdir(ms.path.parent)) == [ms.path.name]
    assert ms.status().restores == 1
    assert ms.status().state == "active"
    assert ms.check("turn") is None


def test_drift_event_has_the_five_fields_and_no_content(ms):
    _tamper_modified(ms)
    ev = ms.check("relay").to_event()
    assert set(ev) == {"trigger", "kind", "expected_sha256", "found_sha256", "restored"}
    assert ev["found_sha256"] == _sha(b"{}")


def test_first_kind_wins_and_all_are_fixed(ms):
    _tamper_modified(ms)
    _tamper_dropin(ms)
    assert ms.check("relay").kind == "modified"
    assert os.listdir(ms.path.parent) == [ms.path.name]


def test_tmp_file_is_not_extra_entry(ms):
    (ms.path.parent / TMP_NAME).write_text("x")
    assert ms.check("relay") is None
    assert (ms.path.parent / TMP_NAME).exists()


def test_check_noop_in_fallback(tmp_path):
    ms = _ms(tmp_path, install=False)
    assert ms.check("relay") is None
    assert not ms.path.exists()


def test_restore_failure_degrades_then_heals(ms, monkeypatch):
    _tamper_modified(ms)

    def boom(path, data):
        raise OSError(13, "denied")

    monkeypatch.setattr(ms_mod, "write_managed", boom)
    drift = ms.check("relay")
    assert drift.restored is False
    assert ms.status().state == "degraded"
    assert ms.active is False
    monkeypatch.undo()
    drift = ms.check("turn")
    assert drift is not None and drift.restored is True
    assert ms.status().state == "active"
    assert ms.active is True


def test_check_after_replace_uses_new_hash(ms):
    ms.replace({"env": {"FOO": "1"}, "hooks": {"PreToolUse": [GROUP]}})
    assert ms.check("relay") is None
    assert json.loads(ms.path.read_bytes())["env"]["FOO"] == "1"
    _tamper_modified(ms)
    ms.check("relay")
    assert json.loads(ms.path.read_bytes())["env"]["FOO"] == "1"


def test_on_drift_called_only_on_drift(ms):
    seen: list[Drift] = []
    ms.on_drift = seen.append
    ms.check("relay")
    assert seen == []
    _tamper_modified(ms)
    ms.check("turn")
    assert [d.kind for d in seen] == ["modified"]


def test_unknown_entries_survive_when_dir_was_not_dedicated(ms):
    ms._dedicated = False
    (ms.path.parent / "keep.txt").write_text("x")
    _tamper_dropin(ms)
    assert ms.check("relay").kind == "extra_entry"
    assert (ms.path.parent / "keep.txt").exists()
    assert not (ms.path.parent / "managed-settings.d").exists()


def test_check_never_deletes_outside_the_managed_dir(tmp_path):
    d = tmp_path / "claude-code"
    d.mkdir()
    (tmp_path / "sibling.txt").write_text("x")
    ms = _ms(tmp_path)
    _tamper_dropin(ms)
    ms.check("relay")
    assert (tmp_path / "sibling.txt").exists()


def test_install_in_non_dedicated_dir_falls_back_and_check_leaves_it(tmp_path):
    d = tmp_path / "home"
    d.mkdir()
    (d / "precious.txt").write_text("x")
    ms = ManagedSettings(d / "managed-settings.json", {"hooks": {}}, RELAY)
    ms.install()
    assert ms.status().state == "fallback"
    assert ms.check("relay") is None
    assert (d / "precious.txt").exists()


def test_install_removes_stale_file_in_fallback(tmp_path):
    d = tmp_path / "home"
    d.mkdir()
    (d / "precious.txt").write_text("x")
    (d / "managed-settings.json").write_text('{"allowManagedHooksOnly": true}')
    ms = ManagedSettings(d / "managed-settings.json", {"hooks": {}}, RELAY)
    st = ms.install()
    assert st.state == "fallback"
    assert not (d / "managed-settings.json").exists()
    assert (d / "precious.txt").exists()


def test_install_reports_stale_file_it_cannot_remove(tmp_path, monkeypatch):
    d = tmp_path / "home"
    d.mkdir()
    (d / "managed-settings.json").write_text("{}")
    monkeypatch.setattr(ManagedSettings, "_remove", staticmethod(_raise_oserror))
    ms = ManagedSettings(d / "managed-settings.json", {"hooks": {}}, RELAY)
    ms.install()
    assert ms.status().state == "fallback"
    assert "stale managed file" in (ms.status().reason or "")


def _raise_oserror(entry):
    raise OSError(13, "denied")


def test_degraded_check_heals_without_drift(ms):
    ms._state = "degraded"
    assert ms.check("relay") is None
    assert ms.active


def test_restore_replaces_a_directory_at_the_path(ms):
    ms.path.unlink()
    ms.path.mkdir()
    (ms.path / "inner").write_text("x")
    drift = ms.check("relay")
    assert (drift.kind, drift.restored) == ("not_regular", True)
    assert ms.path.is_file()


# ---------------------------------------------------------------- manager and routers


async def _manager(world, **scenario):
    cfg = world.cfg(scenario or None)
    m = world.manager(cfg)
    assert m.managed_settings is not None and m.managed_settings.active
    return m, m.managed_settings


def _restored(world, sid):
    return [e for e in world.events(sid) if e["type"] == "managed_settings_restored"]


async def test_manager_wires_on_drift(world):
    cfg = world.cfg()
    ms = world.managed_settings(cfg)
    m = world.manager(cfg, managed_settings=ms)
    assert ms.on_drift == m._on_managed_drift


async def test_manager_check_is_noop_without_managed_settings(world):
    m = world.manager(world.cfg(), managed=False)
    m.check_managed_settings("relay")


async def test_relay_call_restores_and_emits_event(world):
    m, ms = await _manager(world)
    sid = (await m.create({}))["session_id"]
    _tamper_modified(ms)
    transport = httpx.ASGITransport(app=create_internal_app(m), client=("127.0.0.1", 0))
    headers = {HEADER_SESSION: sid, HEADER_TOKEN: m._active[sid].hook_token}
    async with httpx.AsyncClient(transport=transport, base_url="http://ccw") as c:
        r = await c.post(
            "/internal/hooks/PreToolUse",
            json={"tool_name": "Bash", "tool_input": {"command": "ls"}},
            headers=headers,
        )
    assert r.status_code == 200
    (ev,) = _restored(world, sid)
    assert ev["data"]["trigger"] == "relay"
    assert ev["data"]["kind"] == "modified"
    assert ev["data"]["restored"] is True
    assert ms.path.read_bytes() == ms._expected


async def test_unmanaged_relay_event_does_not_check(world):
    m, ms = await _manager(world)
    sid = (await m.create({}))["session_id"]
    other = next(e for e in CLAUDE_HOOK_EVENTS if e not in MANAGED_RELAY_EVENTS)
    _tamper_modified(ms)
    transport = httpx.ASGITransport(app=create_internal_app(m), client=("127.0.0.1", 0))
    headers = {HEADER_SESSION: sid, HEADER_TOKEN: m._active[sid].hook_token}
    async with httpx.AsyncClient(transport=transport, base_url="http://ccw") as c:
        await c.post(f"/internal/hooks/{other}", json={}, headers=headers)
    assert _restored(world, sid) == []


async def test_turn_start_restores_before_turn_started(world):
    m, ms = await _manager(world, turns=[{"match": "hi", "steps": [{"text": "ok"}]}])
    sid = (await m.create({}))["session_id"]
    _tamper_modified(ms)
    await m.router(sid).submit_turn("hi")
    await world.wait_turn(m, sid)
    events = world.events(sid)
    (ev,) = _restored(world, sid)
    assert ev["data"]["trigger"] == "turn"
    started = [e for e in events if e["type"] == "turn_started"]
    assert started and ev["seq"] < started[-1]["seq"]


async def test_event_fans_out_to_all_sessions(world):
    m, ms = await _manager(world)
    a = (await m.create({}))["session_id"]
    b = (await m.create({}))["session_id"]
    _tamper_modified(ms)
    m.check_managed_settings("relay")
    for sid in (a, b):
        (ev,) = _restored(world, sid)
        assert ev["data"]["kind"] == "modified"


async def test_log_names_kind_and_trigger_only(world, caplog):
    m, ms = await _manager(world)
    await m.create({})
    _tamper_modified(ms)
    with caplog.at_level(logging.WARNING):
        m.check_managed_settings("relay")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "modified" in text and "relay" in text
    assert "{}" not in text


async def test_degraded_new_session_gets_full_relay(world, monkeypatch):
    m, ms = await _manager(world)
    _tamper_modified(ms)

    def boom(path, data):
        raise OSError(13, "denied")

    monkeypatch.setattr(ms_mod, "write_managed", boom)
    m.check_managed_settings("relay")
    assert not ms.active
    sid = (await m.create({}))["session_id"]
    doc = json.loads((world.session_dir(sid) / "config" / "settings.json").read_text())
    for event in CLAUDE_HOOK_EVENTS:
        groups = doc["hooks"].get(event, [])
        assert any("ccw-hook" in h.get("command", "") for g in groups for h in g["hooks"]), event


def test_unreadable_file_is_tampered_not_missing(ms, monkeypatch):
    real_open = open
    calls = []

    def flaky(file, *args, **kwargs):
        if Path(file) == ms.path and not calls:
            calls.append(file)
            raise OSError(13, "denied")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(ms_mod, "open", flaky, raising=False)
    drift = ms.check("relay")
    assert drift is not None
    assert (drift.kind, drift.restored) == ("unreadable", True)
    assert ms.check("relay") is None


def test_size_mismatch_read_is_bounded(ms):
    expected = ms.path.read_bytes()
    ms.path.write_bytes(b"x" * (len(expected) + 5000))
    drift = ms.check("relay")
    assert drift.kind == "modified"
    assert drift.found_sha256 == _sha(b"x" * (len(expected) + 1))
    assert ms.path.read_bytes() == expected


def test_persistent_restore_failure_emits_once(ms, monkeypatch):
    seen = []
    ms.on_drift = seen.append
    _tamper_modified(ms)

    def boom(path, data):
        raise OSError(13, "denied")

    monkeypatch.setattr(ms_mod, "write_managed", boom)
    assert ms.check("relay").restored is False
    assert ms.check("relay") is not None
    assert ms.check("turn") is not None
    assert [d.restored for d in seen] == [False]
    monkeypatch.undo()
    assert ms.check("turn").restored is True
    assert [d.restored for d in seen] == [False, True]
