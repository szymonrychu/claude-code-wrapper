"""One timestamp format and no duplicated helpers across modules."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from claude_code_wrapper import events, hooks, metrics, ring, session, turns
from claude_code_wrapper.api import sessions as api_sessions
from claude_code_wrapper.api import turns as api_turns

ROOT = Path(__file__).resolve().parent.parent
RFC3339_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def test_now_iso_is_rfc3339_utc_z():
    assert RFC3339_Z.match(turns.now_iso())


def test_turns_events_hooks_share_one_timestamp_format():
    assert turns.now_iso is events.now_iso is hooks.now_iso


def test_metrics_parse_z_timestamps():
    assert metrics._seconds_between("2026-01-01T00:00:00.000Z", "2026-01-01T00:00:02.500Z") == 2.5


def test_squash_and_out_are_single_definitions():
    assert session._squash is ring._squash
    assert api_sessions._out is api_turns._out


def test_example_config_single_source_and_guard_is_self_contained():
    root_copy = ROOT / "config.example.yaml"
    packaged = ROOT / "src/claude_code_wrapper/data/config.example.yaml"
    assert root_copy.is_symlink() or root_copy.read_bytes() == packaged.read_bytes()
    cfg = yaml.safe_load(packaged.read_text())
    for action in cfg["hooks"]["onAgentToolCall"]:
        assert "guard.sh" not in action["run"]
