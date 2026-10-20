"""Session routes over a recording stub manager (behaviour lives in the manager)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.config import Config
from claude_code_wrapper.manager import (
    CreateFailed,
    CreateSessionRequest,
    CreateVetoed,
    InvalidRequest,
    InvalidState,
    ResumeUnavailable,
    ResumeVetoed,
    SessionNotFound,
    TooManySessions,
)
from claude_code_wrapper.prompts import PromptError
from claude_code_wrapper.redact import Redactor

TOKEN = "tok-0123456789abcdef"
OAUTH = "sk-ant-oat01-supersecretvalue"
H = {"Authorization": f"Bearer {TOKEN}"}


def snap(sid: str = "ses_000000000001", state: str = "ready", **kw: Any) -> dict[str, Any]:
    return {
        "session_id": sid,
        "claude_session_id": "c1",
        "name": "n",
        "state": state,
        "workdir": "/w",
        "workdir_mode": "empty",
        "model": None,
        "effort": None,
        "created_at": "t",
        "last_activity_at": "t",
        "turns_completed": 0,
        "current_turn_id": None,
        "stall_suspected_since": None,
        "last_fatal": None,
        "restarts": 0,
        "env": {},
        "callbackUrl": None,
        **kw,
    }


class StubManager:
    def __init__(self) -> None:
        self.started = True
        self.redactor = Redactor()
        self.calls: list[tuple] = []
        self.raise_on: Exception | None = None

    def _maybe(self) -> None:
        if self.raise_on is not None:
            raise self.raise_on

    async def create(self, req: CreateSessionRequest):
        self.calls.append(("create", req))
        self._maybe()
        return snap(name=req.name, callbackUrl=req.callback_url)

    def list(self, state=None):
        self.calls.append(("list", state))
        return [snap()]

    def get(self, sid):
        self.calls.append(("get", sid))
        self._maybe()
        return snap(sid)

    async def destroy(self, sid, purge=False):
        self.calls.append(("destroy", sid, purge))
        self._maybe()
        return snap(sid, "stopped", **({"purged": True} if purge else {}))

    async def resume(self, sid, env=None):
        self.calls.append(("resume", sid, env))
        self._maybe()
        return snap(sid)


@pytest.fixture
def stub() -> StubManager:
    return StubManager()


@pytest.fixture
def client(stub: StubManager) -> TestClient:
    cfg = Config.model_validate(
        {
            "server": {"auth_token": TOKEN, "callback_secret": "cb-secret-0123456789"},
            "claude": {"env": {"claude_code_oauth_token": OAUTH}},
            "prompts": {"templates": {"greet": {"description": "d", "text": "hi"}}},
        }
    )
    return TestClient(create_app(stub, cfg), headers=H)


def test_create_returns_201_snapshot(client: TestClient, stub: StubManager) -> None:
    r = client.post(
        "/v1/sessions",
        json={
            "name": "a",
            "callbackUrl": "https://example.test/cb",
            "env": {"extra": {"FOO": "1"}},
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "a" and body["callbackUrl"] == "https://example.test/cb"
    req = stub.calls[0][1]
    assert req.env.extra == {"FOO": "1"}


def test_create_rejects_non_http_callback_url(client: TestClient) -> None:
    r = client.post("/v1/sessions", json={"callbackUrl": "ftp://x/y"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_callback_url"


def test_create_rejects_snake_case_callback_url(client: TestClient) -> None:
    r = client.post("/v1/sessions", json={"callback_url": "http://x/y"})
    assert r.status_code == 422


def test_create_unknown_env_var_names_it(client: TestClient) -> None:
    r = client.post("/v1/sessions", json={"env": {"not_a_var": "1"}})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "invalid_request"
    assert "env.not_a_var" in err["message"]


def test_create_managed_var_names_it(client: TestClient) -> None:
    r = client.post("/v1/sessions", json={"env": {"extra": {"CLAUDE_CONFIG_DIR": "/x"}}})
    assert r.status_code == 422
    assert "CLAUDE_CONFIG_DIR" in r.json()["error"]["message"]
    r = client.post("/v1/sessions", json={"env": {"claude_config_dir": "/x"}})
    assert r.status_code == 422
    assert "CLAUDE_CONFIG_DIR" in r.json()["error"]["message"]


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (SessionNotFound("ses_x"), 404, "not_found"),
        (TooManySessions(2), 429, "too_many_sessions"),
        (CreateVetoed("no"), 403, "vetoed"),
        (ResumeVetoed("no"), 403, "vetoed"),
        (InvalidState("ses_x", "busy"), 409, "invalid_state"),
        (ResumeUnavailable("ses_x", "no transcript"), 409, "not_resumable"),
        (CreateFailed("install_workdir", "boom"), 500, "create_failed"),
        (InvalidRequest("env: bad"), 422, "invalid_request"),
        (PromptError("text_or_template"), 400, "text_or_template"),
        (PromptError("unknown_template", "zz"), 422, "unknown_template"),
    ],
)
def test_error_mapping(client: TestClient, stub: StubManager, exc, status, code) -> None:
    stub.raise_on = exc
    r = client.post("/v1/sessions", json={})
    assert r.status_code == status
    assert r.json()["error"]["code"] == code


def test_create_failed_message_includes_step(client: TestClient, stub: StubManager) -> None:
    stub.raise_on = CreateFailed("install_workdir", "boom")
    assert "install_workdir" in client.post("/v1/sessions", json={}).json()["error"]["message"]


def test_error_messages_are_redacted(client: TestClient, stub: StubManager) -> None:
    stub.raise_on = CreateFailed("x", f"leaked {OAUTH} and {TOKEN}")
    msg = client.post("/v1/sessions", json={}).json()["error"]["message"]
    assert OAUTH not in msg and TOKEN not in msg and "***" in msg


def test_list_with_state_filter(client: TestClient, stub: StubManager) -> None:
    r = client.get("/v1/sessions", params={"state": "busy"})
    assert r.status_code == 200 and r.json() == {"sessions": [snap()]}
    assert stub.calls[-1] == ("list", "busy")
    assert client.get("/v1/sessions").status_code == 200
    assert stub.calls[-1] == ("list", None)


def test_list_rejects_unknown_state(client: TestClient) -> None:
    r = client.get("/v1/sessions", params={"state": "bogus"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"
    assert "state" in r.json()["error"]["message"]


def test_get_snapshot_and_404(client: TestClient, stub: StubManager) -> None:
    body = client.get("/v1/sessions/ses_000000000001").json()
    assert set(body) >= {
        "session_id", "claude_session_id", "name", "state", "workdir", "workdir_mode", "model",
        "effort", "created_at", "last_activity_at", "turns_completed", "current_turn_id",
        "stall_suspected_since", "last_fatal", "restarts", "env", "callbackUrl",
    }  # fmt: skip
    stub.raise_on = SessionNotFound("ses_zz")
    assert client.get("/v1/sessions/ses_zz").status_code == 404


def test_delete_default_keeps_files_and_purge(client: TestClient, stub: StubManager) -> None:
    r = client.delete("/v1/sessions/ses_000000000001")
    assert r.status_code == 200 and r.json()["state"] == "stopped"
    assert stub.calls[-1] == ("destroy", "ses_000000000001", False)
    r = client.delete("/v1/sessions/ses_000000000001", params={"purge": "true"})
    assert r.json()["purged"] is True
    assert stub.calls[-1] == ("destroy", "ses_000000000001", True)


def test_resume_passes_env_and_defaults_empty(client: TestClient, stub: StubManager) -> None:
    assert client.post("/v1/sessions/ses_000000000001/resume").status_code == 200
    assert stub.calls[-1][2] is None
    r = client.post(
        "/v1/sessions/ses_000000000001/resume",
        json={"env": {"claude_code_oauth_token": OAUTH}},
    )
    assert r.status_code == 200
    assert stub.calls[-1][2].claude_code_oauth_token.get_secret_value() == OAUTH
    assert OAUTH not in r.text


def test_config_is_redacted(client: TestClient) -> None:
    r = client.get("/v1/config")
    assert r.status_code == 200
    assert OAUTH not in r.text and TOKEN not in r.text and "cb-secret-0123456789" not in r.text


def test_prompts_lists_templates(client: TestClient) -> None:
    body = client.get("/v1/prompts").json()
    assert [t["name"] for t in body["templates"]] == ["greet"]


def test_snapshot_secret_never_leaks(client: TestClient, stub: StubManager) -> None:
    stub.redactor.add([OAUTH])
    stub.get = lambda sid: snap(sid, last_fatal=f"boom {OAUTH}")  # type: ignore[method-assign]
    assert OAUTH not in client.get("/v1/sessions/ses_000000000001").text
