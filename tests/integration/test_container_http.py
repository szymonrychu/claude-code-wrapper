"""The real container over HTTP, driven by the scripted fake claude."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from .conftest import AUTH_TOKEN, CALLBACK_SECRET

pytestmark = pytest.mark.integration

DEADLINE = 60.0
PROMPT = "run the ci lifecycle"


def wait_for(probe: Callable[[], Any], what: str, timeout: float = DEADLINE) -> Any:
    end = time.monotonic() + timeout
    while True:
        value = probe()
        if value:
            return value
        if time.monotonic() > end:
            raise AssertionError(f"timed out after {timeout:g}s waiting for {what}")
        time.sleep(0.2)


def test_health_and_ready(anon: httpx.Client) -> None:
    assert anon.get("/healthz").status_code == 200
    assert anon.get("/readyz").status_code == 200


@pytest.mark.parametrize("path", ["/v1/sessions", "/v1/config", "/v1/prompts"])
def test_public_routes_need_bearer(anon: httpx.Client, api: httpx.Client, path: str) -> None:
    assert anon.get(path).status_code == 401
    assert anon.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert api.get(path).status_code == 200


def test_config_is_redacted(api: httpx.Client) -> None:
    resp = api.get("/v1/config")
    assert resp.status_code == 200
    assert AUTH_TOKEN not in resp.text
    assert CALLBACK_SECRET not in resp.text
    body = resp.json()
    assert body["server"]["auth_token"] == "***"
    assert body["server"]["callback_secret"] == "***"
    assert body["claude"]["binary"] == "/opt/ci/claude"


def test_sessions_start_empty(api: httpx.Client) -> None:
    assert api.get("/v1/sessions").json() == {"sessions": []}


def test_full_session_lifecycle(api: httpx.Client) -> None:
    created = api.post("/v1/sessions", json={"name": "ci"})
    assert created.status_code == 201, created.text
    sid = created.json()["session_id"]
    try:
        ready = wait_for(
            lambda: (
                (s := api.get(f"/v1/sessions/{sid}").json())["state"] in ("ready", "failed") and s
            ),
            "session ready",
        )
        assert ready["state"] == "ready", ready

        submitted = api.post(f"/v1/sessions/{sid}/turns", json={"text": PROMPT})
        assert submitted.status_code == 202, submitted.text
        tid = submitted.json()["turn_id"]
        record = wait_for(
            lambda: (
                (r := api.get(f"/v1/sessions/{sid}/turns/{tid}").json())["state"] != "running" and r
            ),
            "turn finished",
        )
        assert record["state"] == "completed", record

        transcript = api.get(f"/v1/sessions/{sid}/transcript")
        assert transcript.status_code == 200
        assert "CI work done." in transcript.text

        with api.stream("GET", f"/v1/sessions/{sid}/events") as stream:
            assert stream.status_code == 200
            assert stream.headers["content-type"].startswith("text/event-stream")
    finally:
        destroyed = api.delete(f"/v1/sessions/{sid}")
    assert destroyed.status_code == 200, destroyed.text
    assert destroyed.json()["state"] == "stopped"
    assert api.get(f"/v1/sessions/{sid}").json()["state"] == "stopped"
    assert sid not in [
        s["session_id"] for s in api.get("/v1/sessions?state=ready").json()["sessions"]
    ]
