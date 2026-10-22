"""Two-container peer fixtures: httpx clients on both published ports, and a compose helper.

Run through `mise run test:integration:peers`, which brings the compose stack up first. Outside
that task an unreachable service skips; inside it (CCW_INTEGRATION_TASK=1) it is a hard failure.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

A_URL = os.environ.get("CCW_PEER_A_URL", "http://127.0.0.1:8181")
B_URL = os.environ.get("CCW_PEER_B_URL", "http://127.0.0.1:8182")
AUTH_TOKEN = "ci-token"
ROOT = Path(__file__).resolve().parents[2]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    here = Path(__file__).parent
    for item in items:
        if here in item.path.parents:
            item.add_marker(pytest.mark.integration)


def pytest_deselected(items: list[pytest.Item]) -> None:
    """Say so even under -qq, so a default run visibly leaves these tests out."""
    ours = [i for i in items if Path(__file__).parent in i.path.parents]
    if ours:
        print(f"{len(ours)} deselected (integration: run `mise run test:integration`)")


def _unreachable(message: str) -> None:
    if os.environ.get("CCW_INTEGRATION_TASK") == "1":
        pytest.fail(message, pytrace=False)
    raise pytest.skip.Exception(message)


@pytest.fixture(scope="session", autouse=True)
def services() -> None:
    for url in (A_URL, B_URL):
        try:
            httpx.get(f"{url}/healthz", timeout=5).raise_for_status()
        except httpx.HTTPError as exc:
            _unreachable(f"service at {url} is unreachable: {exc}")


@pytest.fixture(scope="session")
def client_a() -> Iterator[httpx.Client]:
    headers = {"Authorization": f"Bearer {AUTH_TOKEN}"}
    with httpx.Client(base_url=A_URL, headers=headers, timeout=10) as client:
        yield client


@pytest.fixture(scope="session")
def client_b() -> Iterator[httpx.Client]:
    headers = {"Authorization": f"Bearer {AUTH_TOKEN}"}
    with httpx.Client(base_url=B_URL, headers=headers, timeout=10) as client:
        yield client


@pytest.fixture(scope="session")
def compose() -> Callable[..., str]:
    """`compose("exec", "-T", "ccw-a", "cat", "/x")` runs docker compose; returns stdout."""
    args = os.environ.get("CCW_PEERS_COMPOSE_ARGS", "")
    if not args:
        _unreachable("CCW_PEERS_COMPOSE_ARGS is not set (run scripts/integration-peers.sh)")

    def run(*extra: str, check: bool = True) -> str:
        proc = subprocess.run(
            ["docker", "compose", *shlex.split(args), *extra],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if check and proc.returncode != 0:
            raise AssertionError(f"docker compose {' '.join(extra)} failed: {proc.stderr}")
        return proc.stdout

    return run
