"""Bearer auth on /v1, open probes, and the error envelope."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

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
    cfg = Config.model_validate({"server": {"auth_token": token, "allow_anonymous": not token}})
    return TestClient(create_app(StubManager(started), cfg))


def test_missing_token_401() -> None:
    r = client().get("/v1/sessions")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert r.json()["error"]["code"] == "unauthorized"
    assert isinstance(r.json()["error"]["message"], str)


@pytest.mark.parametrize("header", ["Bearer wrong", "Basic " + TOKEN, TOKEN, "Bearer "])
def test_wrong_token_401(header: str) -> None:
    r = client().get("/v1/sessions", headers={"Authorization": header})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_right_token_passes() -> None:
    r = client().get("/v1/sessions", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200


def test_health_unauthenticated() -> None:
    c = client()
    assert c.get("/healthz").json() == {"status": "ok"}
    assert c.get("/readyz").json() == {"status": "ready"}


def test_readyz_503_until_started() -> None:
    r = client(started=False).get("/readyz")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "not_ready"


def test_allow_anonymous_opens_v1(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        c = client(token="")
        assert c.get("/v1/sessions").status_code == 200
        assert c.get("/v1/sessions").status_code == 200
    assert len([r for r in caplog.records if "auth_token" in r.getMessage()]) == 1


def test_no_openapi_routes() -> None:
    c = client()
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(path).status_code == 404


def test_unknown_route_uses_envelope() -> None:
    r = client().get("/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_empty_token_refused_without_allow_anonymous() -> None:
    from claude_code_wrapper.config import ConfigError

    with pytest.raises(ConfigError) as exc:
        create_app(StubManager(), Config.model_validate({}))
    assert "server.auth_token" in str(exc.value)
    assert "server.allow_anonymous" in str(exc.value)


def test_probes_and_metrics_stay_open_with_token() -> None:
    c = client()
    assert c.get("/healthz").status_code == 200
    assert c.get("/readyz").status_code == 200
    assert c.get("/metrics").status_code == 200


ATTACH = "/v1/sessions/ses_aaaaaaaaaaaa/terminals/{}/attach"


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic " + TOKEN}]
)
def test_websocket_requires_bearer(headers: dict[str, str]) -> None:
    c = client()
    with (
        pytest.raises(WebSocketDenialResponse) as exc,
        c.websocket_connect(ATTACH.format("term_0123456789ab"), headers=headers),
    ):
        pass
    assert exc.value.status_code == 401
    assert exc.value.headers["www-authenticate"] == "Bearer"
    assert exc.value.json()["error"]["code"] == "unauthorized"
    # The right token gets past auth to the route (which refuses the malformed id).
    with (
        pytest.raises(WebSocketDenialResponse) as exc,
        c.websocket_connect(ATTACH.format("nope"), headers={"Authorization": f"Bearer {TOKEN}"}),
    ):
        pass
    assert exc.value.status_code == 422
    assert exc.value.json()["error"]["code"] == "invalid_request"
    # HTTP auth is unchanged.
    assert c.get("/v1/sessions", headers=headers).status_code == 401
    assert c.get("/v1/sessions", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
