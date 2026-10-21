"""Integration fixtures: httpx clients against the running container (CCW_BASE_URL).

Run through `mise run test:integration`, which brings the compose stack up first. Outside that
task an unreachable service skips; inside it (CCW_INTEGRATION_TASK=1) it is a hard failure.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

BASE_URL = os.environ.get("CCW_BASE_URL", "http://127.0.0.1:8080")
AUTH_TOKEN = "ci-token"
CALLBACK_SECRET = "ci-callback-secret"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    here = Path(__file__).parent
    for item in items:
        if here in item.path.parents:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session", autouse=True)
def service() -> str:
    try:
        httpx.get(f"{BASE_URL}/healthz", timeout=5).raise_for_status()
    except httpx.HTTPError as exc:
        message = f"service at {BASE_URL} is unreachable: {exc}"
        if os.environ.get("CCW_INTEGRATION_TASK") == "1":
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    return BASE_URL


@pytest.fixture
def anon(service: str) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=service, timeout=10) as client:
        yield client


@pytest.fixture
def api(service: str) -> Iterator[httpx.Client]:
    headers = {"Authorization": f"Bearer {AUTH_TOKEN}"}
    with httpx.Client(base_url=service, headers=headers, timeout=10) as client:
        yield client
