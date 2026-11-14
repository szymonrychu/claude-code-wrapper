"""GET and PUT /v1/managed-settings: read and replace the overlay in flight (spec D2)."""

from __future__ import annotations

import hashlib
import json
import logging
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from claude_code_wrapper import managed_settings as ms_mod
from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.api.managed_settings import MAX_MANAGED_BODY_BYTES
from claude_code_wrapper.bootstrap.settings import render_managed_settings
from claude_code_wrapper.config import Config
from claude_code_wrapper.managed_settings import ManagedSettings
from claude_code_wrapper.redact import Redactor

TOKEN = "tok-0123456789abcdef"  # gitleaks:allow
AUTH = {"Authorization": f"Bearer {TOKEN}"}
RELAY = "/x/ccw-hook"
CANARY = "CANARY-mset-7f3a"
GROUP = {"matcher": "Bash", "hooks": [{"type": "command", "command": "/bin/true"}]}
OVERLAY = {"env": {"FOO": "1"}, "hooks": {"PreToolUse": [GROUP]}}


class StubManager:
    started = True

    def __init__(self, managed: ManagedSettings | None) -> None:
        self.redactor = Redactor()
        self.managed_settings = managed


def _ms(tmp_path: Path, install: bool = True) -> ManagedSettings:
    d = tmp_path / "claude-code"
    d.mkdir(exist_ok=True)
    ms = ManagedSettings(
        d / "managed-settings.json",
        render_managed_settings(Config(), RELAY, Path("/etc/claude-code")),
        RELAY,
    )
    if install:
        ms.install()
    return ms


def _client(ms: ManagedSettings | None) -> TestClient:
    cfg = Config.model_validate({"server": {"auth_token": TOKEN}})
    return TestClient(create_app(StubManager(ms), cfg))


@pytest.fixture
def ms(tmp_path):
    return _ms(tmp_path)


@pytest.fixture
def client(ms):
    return _client(ms)


def _put(client, overlay=None, **kw):
    return client.put("/v1/managed-settings", json={"overlay": overlay or {}}, headers=AUTH, **kw)


def test_get_reports_active_document(client, ms):
    r = client.get("/v1/managed-settings", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    raw = ms.path.read_bytes()
    assert body["state"] == "active"
    assert body["path"] == str(ms.path)
    assert body["sha256"] == hashlib.sha256(raw).hexdigest()
    assert body["overlay"] == {}
    assert body["restores"] == 0
    assert body["document"] == json.loads(raw)
    assert f"{RELAY} PreToolUse" in json.dumps(body["document"]["hooks"]["PreToolUse"])


def test_put_merges_overlay_and_rewrites_file(client, ms):
    before = client.get("/v1/managed-settings", headers=AUTH).json()["sha256"]
    r = _put(client, OVERLAY)
    assert r.status_code == 200
    doc = json.loads(ms.path.read_text())
    assert doc["env"]["FOO"] == "1"
    assert doc["env"]["CLAUDE_CODE_EXPERIMENTAL_CODE_MODE"] == "0"
    assert doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == f"{RELAY} PreToolUse"
    assert doc["hooks"]["PreToolUse"][1] == GROUP
    assert stat.S_IMODE(ms.path.stat().st_mode) == 0o644
    got = client.get("/v1/managed-settings", headers=AUTH).json()
    assert got["sha256"] != before
    assert got["sha256"] == hashlib.sha256(ms.path.read_bytes()).hexdigest()
    assert got["overlay"] == OVERLAY
    assert r.json() == got


def test_put_replaces_not_patches(client, ms):
    _put(client, {"env": {"FOO": "1"}})
    assert _put(client, {}).status_code == 200
    assert "FOO" not in json.loads(ms.path.read_text())["env"]
    assert client.get("/v1/managed-settings", headers=AUTH).json()["overlay"] == {}


@pytest.mark.parametrize(
    "overlay",
    [
        {"disableAgentView": False},
        {
            "pluginConfigs": {
                "cc-plugin-sec-default@builtin": {"options": {"allowManagedModsOnly": False}}
            }
        },
        {"env": {"CLAUDE_CODE_EXPERIMENTAL_CODE_MODE": "1"}},
        {"env": {"CLAUDE_CODE_DISABLE_AGENT_VIEW": "0"}},
        {"disableAllHooks": True},
        {"allowManagedHooksOnly": True},
    ],
)
def test_put_locked_key_refused(client, ms, overlay):
    before = ms.path.read_bytes()
    r = _put(client, overlay)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "locked_key"
    assert ms.path.read_bytes() == before


def test_put_false_kill_switch_accepted_not_written_true(client, ms):
    r = _put(client, {"disableAllHooks": False, "allowManagedHooksOnly": False})
    assert r.status_code == 200
    doc = json.loads(ms.path.read_text())
    assert doc["disableAllHooks"] is False
    assert doc["allowManagedHooksOnly"] is False


def test_put_deny_cannot_drop_baseline(client, ms):
    baseline = json.loads(ms.path.read_text())["permissions"]["deny"]
    assert _put(client, {"permissions": {"deny": ["Bash(rm:*)"]}}).status_code == 200
    assert json.loads(ms.path.read_text())["permissions"]["deny"] == [*baseline, "Bash(rm:*)"]


def test_put_secret_env_refused(client, ms, caplog):
    caplog.set_level(logging.DEBUG)
    before = ms.path.read_bytes()
    r = _put(client, {"env": {"GH_TOKEN": CANARY}})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "secret_in_overlay"
    assert CANARY not in r.text
    assert "GH_TOKEN" not in r.text
    assert CANARY not in caplog.text
    assert CANARY.encode() not in ms.path.read_bytes()
    assert ms.path.read_bytes() == before


def test_put_in_fallback_409(tmp_path):
    ms = _ms(tmp_path, install=False)  # state stays fallback
    r = _put(_client(ms), OVERLAY)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "managed_settings_unavailable"
    assert not ms.path.exists()


def test_put_without_managed_settings_409():
    r = _put(_client(None), OVERLAY)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "managed_settings_unavailable"
    assert _client(None).get("/v1/managed-settings", headers=AUTH).status_code == 409


def test_put_in_degraded_heals(client, ms):
    ms._state = "degraded"
    ms.path.write_text("tampered")
    r = _put(client, {"env": {"FOO": "1"}})
    assert r.status_code == 200
    assert r.json()["state"] == "active"
    assert r.json()["restores"] == 1
    assert json.loads(ms.path.read_text())["env"]["FOO"] == "1"


def test_get_in_degraded_heals(client, ms):
    ms._state = "degraded"
    ms.path.write_text("tampered")
    r = client.get("/v1/managed-settings", headers=AUTH)
    assert r.json()["state"] == "active"
    assert ms.path.read_bytes() == ms._expected


def test_put_in_degraded_409_when_heal_fails(client, ms, monkeypatch):
    ms._state = "degraded"

    def boom(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(ms_mod, "write_managed", boom)
    r = _put(client, OVERLAY)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "managed_settings_unavailable"


def test_put_too_large_413(client, ms):
    before = ms.path.read_bytes()
    r = client.put(
        "/v1/managed-settings",
        content=json.dumps({"overlay": {"x": "a" * MAX_MANAGED_BODY_BYTES}}),
        headers={**AUTH, "content-type": "application/json"},
    )
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "too_large"
    assert ms.path.read_bytes() == before


def test_put_requires_bearer_401(client, ms):
    before = ms.path.read_bytes()
    r = client.put("/v1/managed-settings", json={"overlay": OVERLAY})
    assert r.status_code == 401
    assert client.get("/v1/managed-settings").status_code == 401
    assert ms.path.read_bytes() == before


@pytest.mark.parametrize(
    "body", [{"overlay": "x"}, {"overlay": [1]}, {"other": {}}, [], {"overlay": {}, "x": 1}]
)
def test_put_rejects_non_object_overlay_422(client, body):
    r = client.put("/v1/managed-settings", json=body, headers=AUTH)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"


def test_put_malformed_hooks_422(client):
    r = _put(client, {"hooks": {"PreToolUse": "x"}})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_overlay"


def test_put_write_failure_keeps_old(client, ms, monkeypatch):
    first = _put(client, {"env": {"FOO": "1"}}).json()
    before = ms.path.read_bytes()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(ms_mod, "write_managed", boom)
    r = _put(client, {"env": {"BAR": "2"}})
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "managed_settings_write_failed"
    monkeypatch.undo()
    got = client.get("/v1/managed-settings", headers=AUTH).json()
    assert got["sha256"] == first["sha256"]
    assert got["overlay"] == {"env": {"FOO": "1"}}
    assert ms.path.read_bytes() == before
    assert ms._expected == before


@pytest.mark.parametrize(
    "overlay",
    [
        {"permissions": {"deny": None}},
        {"permissions": {"deny": "Bash"}},
        {"permissions": {"deny": {"a": 1}}},
        {"permissions": [1]},
        {"permissions": "s"},
        {"permissions": None},
        {"hooks": None},
        {"hooks": []},
        {"hooks": "x"},
        {"hooks": {"PreToolUse": None}},
        {"hooks": {"PreToolUse": {"a": 1}}},
    ],
)
def test_put_malformed_shapes_422_invalid_overlay(client, ms, overlay):
    before = ms.document
    r = _put(client, overlay)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_overlay"
    assert ms.document == before


def test_view_is_one_snapshot_under_the_lock(ms):
    snap = ms.snapshot()
    assert snap.status.sha256 and snap.document == ms.document
