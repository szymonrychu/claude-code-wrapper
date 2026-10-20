"""Bearer auth on /v1, open probes, and the error envelope."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.config import Config
from claude_code_wrapper.redact import Redactor

TOKEN = "tok-0123456789abcdef"


class StubManager:
    def __init__(self, started: bool = True) -> None:
        self.started = started
        self.redactor = Redactor()

    def list(self, state=None):
        return []


def client(token: str = TOKEN, started: bool = True) -> TestClient:
    cfg = Config.model_validate({"server": {"auth_token": token}})
    return TestClient(create_app(StubManager(started), cfg))


def test_missing_token_is_401_with_envelope_and_challenge() -> None:
    r = client().get("/v1/sessions")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert r.json()["error"]["code"] == "unauthorized"
    assert isinstance(r.json()["error"]["message"], str)


@pytest.mark.parametrize("header", ["Bearer wrong", "Basic " + TOKEN, TOKEN, "Bearer "])
def test_wrong_token_is_401(header: str) -> None:
    r = client().get("/v1/sessions", headers={"Authorization": header})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_right_token_passes() -> None:
    r = client().get("/v1/sessions", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200


def test_probes_are_open() -> None:
    c = client()
    assert c.get("/healthz").json() == {"status": "ok"}
    assert c.get("/readyz").json() == {"status": "ready"}


def test_readyz_503_until_started() -> None:
    r = client(started=False).get("/readyz")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "not_ready"


def test_empty_token_disables_auth_and_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        c = client(token="")
        assert c.get("/v1/sessions").status_code == 200
        assert c.get("/v1/sessions").status_code == 200
    assert len([r for r in caplog.records if "auth_token" in r.getMessage()]) == 1


def test_docs_and_openapi_are_off() -> None:
    c = client()
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(path).status_code == 404


def test_unknown_route_uses_envelope() -> None:
    r = client().get("/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
