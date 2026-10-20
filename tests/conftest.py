"""Shared fixtures: the scripted fake claude, scenario files and a config dir."""

from __future__ import annotations

import json
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

FAKE_CLAUDE = Path(__file__).parent / "fake_claude" / "fake_claude.py"


@pytest.fixture
def fake_claude_bin(tmp_path: Path) -> Path:
    """An executable `claude` shim that runs the fake under the current interpreter."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    shim = bin_dir / "claude"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{FAKE_CLAUDE}" "$@"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


@pytest.fixture
def scenario_file(tmp_path: Path) -> Callable[[dict], Path]:
    """Factory: write a scenario dict to JSON and return the path."""
    counter = 0

    def make(scenario: dict) -> Path:
        nonlocal counter
        counter += 1
        path = tmp_path / f"scenario-{counter}.json"
        path.write_text(json.dumps(scenario))
        return path

    return make


@pytest.fixture
def claude_config_dir(tmp_path: Path) -> Path:
    path = tmp_path / "claude-config"
    path.mkdir()
    return path
