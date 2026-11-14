"""live-claude fixtures: the built image running real Claude Code (scripts/live-claude.sh).

The script starts the fake Messages API on the host and the container on the host network,
then runs `uv run pytest -m live tests/live` with:

- CCW_BASE_URL: the wrapper (default http://127.0.0.1:8080);
- LIVE_API_LOG: the fake API's JSON-lines request log (host path);
- LIVE_EXEC: the command prefix that runs a command in the container (default
  `docker exec ccw-live`);
- LIVE_TASK=1: an unreachable service fails instead of skipping.

The managed overlay is PUT once per run, before the first session starts, so every session
starts with the managed test mod enabled (Claude Code reads managed settings at start).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

BASE_URL = os.environ.get("CCW_BASE_URL", "http://127.0.0.1:8080")
AUTH_TOKEN = "live-token"
API_LOG = Path(os.environ.get("LIVE_API_LOG", "/tmp/ccw-live-api.jsonl"))
EXEC = shlex.split(os.environ.get("LIVE_EXEC", "docker exec ccw-live"))
MODS_DIR = os.environ.get("LIVE_MODS_DIR", "/opt/live-mods")
MANAGED_DIR = str(Path(os.environ.get("CCW_MANAGED_SETTINGS", "/etc/claude-code/x")).parent)
TURN_TIMEOUT = 120.0

MOD_ID = "live-managed-mod@ccw-live"
#: The managed overlay of the run (spec Q2): the test mod counts as the organization's only
#: when managed settings declare its directory marketplace, enable it and list it, with the
#: built-in guard kept in prependPlugins (mods admin docs, 2.1.323). The managed dir is an
#: additional directory so the write fence cannot refuse a Write there: only the managed deny
#: rule can (ruling R9).
OVERLAY: dict[str, Any] = {
    "extraKnownMarketplaces": {
        "ccw-live": {"source": {"source": "directory", "path": f"{MODS_DIR}/managed-test-mod"}}
    },
    "enabledPlugins": {MOD_ID: True},
    "prependPlugins": [MOD_ID, "sec-default@builtin"],
    "permissions": {"additionalDirectories": [MANAGED_DIR]},
}

MANAGED_MOD_MARKER = "/tmp/ccw-live-managed-mod.marker"
UNMANAGED_MOD_MARKER = "/tmp/ccw-live-unmanaged-mod.marker"
SETTINGS_HOOK_LOG = "/tmp/ccw-live-settings-pretooluse.jsonl"
STATUSLINE_MARKER = "/tmp/ccw-live-statusline.json"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    here = Path(__file__).parent
    for item in items:
        if here in item.path.parents:
            item.add_marker(pytest.mark.live)


def wait_for(probe: Callable[[], Any], what: str, timeout: float = TURN_TIMEOUT) -> Any:
    end = time.monotonic() + timeout
    while True:
        value = probe()
        if value:
            return value
        if time.monotonic() > end:
            raise AssertionError(f"timed out after {timeout:g}s waiting for {what}")
        time.sleep(0.3)


def in_container(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a command in the container (LIVE_EXEC prefix)."""
    return subprocess.run([*EXEC, *args], capture_output=True, text=True, check=check, timeout=60)


def container_file(path: str) -> str | None:
    """The text of a file in the container, None when it does not exist."""
    out = in_container("cat", path, check=False)
    return out.stdout if out.returncode == 0 else None


def api_log() -> list[dict[str, Any]]:
    if not API_LOG.exists():
        return []
    return [json.loads(line) for line in API_LOG.read_text().splitlines() if line.strip()]


def tool_results(case: str) -> list[dict[str, Any]]:
    """The tool_results Claude Code sent back to the fake API for `case`."""
    return [r for rec in api_log() if rec["case"] == case for r in rec["tool_results"]]


def live_prompt(
    case: str, tool: str | None = None, tool_input: dict[str, Any] | None = None
) -> str:
    text = f"LIVE-CASE:{case}"
    if tool is not None:
        call = {"name": tool, "input": tool_input or {}}
        text += " LIVE-TOOL:" + json.dumps(call, separators=(",", ":"))
    return text


class Live:
    """The wrapper API with the helpers the live cases share."""

    def __init__(self, client: httpx.Client) -> None:
        self.api = client

    def create_session(self, name: str) -> dict[str, Any]:
        created = self.api.post("/v1/sessions", json={"name": name}, timeout=180)
        assert created.status_code == 201, created.text
        sid = created.json()["session_id"]
        ready = wait_for(
            lambda: (
                (s := self.api.get(f"/v1/sessions/{sid}").json())["state"] in ("ready", "failed")
                and s
            ),
            f"session {name} ready",
        )
        assert ready["state"] == "ready", ready
        return ready

    def turn(self, sid: str, text: str, timeout: float = TURN_TIMEOUT) -> dict[str, Any]:
        """Submit a turn and wait until it leaves `running`; the final turn record."""
        submitted = self.api.post(f"/v1/sessions/{sid}/turns", json={"text": text})
        assert submitted.status_code == 202, submitted.text
        tid = submitted.json()["turn_id"]
        return wait_for(
            lambda: (
                (r := self.api.get(f"/v1/sessions/{sid}/turns/{tid}").json())["state"]
                not in ("pending", "running")
                and r
            ),
            f"turn {text[:40]!r} finished",
            timeout,
        )

    def events(self, sid: str, types: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        after = 0
        while True:
            page = self.api.get(
                f"/v1/sessions/{sid}/history",
                params={"types": types, "after_seq": after, "limit": 500},
            ).json()
            out.extend(page["events"])
            if not page["has_more"]:
                return out
            after = page["next_seq"]


@pytest.fixture(scope="session")
def live() -> Iterator[Live]:
    try:
        httpx.get(f"{BASE_URL}/healthz", timeout=5).raise_for_status()
    except httpx.HTTPError as exc:
        message = f"service at {BASE_URL} is unreachable: {exc}"
        if os.environ.get("LIVE_TASK") == "1":
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    headers = {"Authorization": f"Bearer {AUTH_TOKEN}"}
    with httpx.Client(base_url=BASE_URL, headers=headers, timeout=30) as client:
        put = client.put("/v1/managed-settings", json={"overlay": OVERLAY})
        assert put.status_code == 200, put.text
        yield Live(client)


@pytest.fixture(scope="session")
def session(live: Live) -> dict[str, Any]:
    """The run's shared session, started after the managed overlay is in place."""
    return live.create_session("live")


@pytest.fixture(scope="session")
def mod_turns(live: Live, session: dict[str, Any]) -> dict[str, Any]:
    """The mod cases: a plain Bash call the managed mod passes on (LIVE-CASE:bash-plain, the
    positive control of ruling R10), then one it answers itself (LIVE-CASE:bash-mod)."""
    sid = session["session_id"]
    plain = live.turn(
        sid, live_prompt("bash-plain", "Bash", {"command": "echo live-plain", "description": "x"})
    )
    modded = live.turn(
        sid, live_prompt("bash-mod", "Bash", {"command": "echo live-mod", "description": "x"})
    )
    return {"session": session, "plain": plain, "mod": modded}
