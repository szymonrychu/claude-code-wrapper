"""Delivery lanes on `POST /v1/sessions/{id}/turns` (agents-ui-improvements C1-C5)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_api_turns import SID, StubManager, client, stub  # noqa: F401 - fixtures

from claude_code_wrapper.api.turns import LANES, TurnRequest


def post(client: TestClient, body: dict[str, Any]):  # noqa: F811
    return client.post(f"/v1/sessions/{SID}/turns", json=body)


def test_lane_values_and_default_regular() -> None:
    assert LANES == ("idle", "regular", "priority")
    assert TurnRequest(text="hi").lane == "regular"
    for lane in LANES:
        assert TurnRequest(text="hi", lane=lane).lane == lane


@pytest.mark.parametrize("lane", ["urgent", "", "IDLE", 1, None])
def test_unknown_lane_is_422(client: TestClient, stub: StubManager, lane: Any) -> None:  # noqa: F811
    r = post(client, {"text": "hi", "lane": lane})
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "invalid_request" and "lane" in body["message"]
    assert stub.router_obj.calls == []
