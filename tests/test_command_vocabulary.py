"""Command and compaction vocabulary: hook events, veto decisions, metrics."""

from __future__ import annotations

from test_metrics import sample

from claude_code_wrapper.config import HOOK_EVENTS, VETO_EVENTS, HooksConfig
from claude_code_wrapper.hooks import EVENT_DECISIONS
from claude_code_wrapper.metrics import Metrics


def test_hook_events_and_decisions():
    assert HOOK_EVENTS[-3:] == ("onCommand", "onCommandComplete", "onError")
    assert "onCommand" in VETO_EVENTS and "onCommandComplete" not in VETO_EVENTS
    assert EVENT_DECISIONS["onCommand"] == ("allow", "deny")
    assert EVENT_DECISIONS["onCommandComplete"] == ("allow",)
    cfg = HooksConfig.model_validate(
        {"onCommand": [{"type": "command", "run": "true"}], "onCommandComplete": []}
    )
    assert len(cfg.onCommand) == 1


def test_command_metrics_bounded():
    m = Metrics()
    m.command_finished("compact", "agent", "completed", 2.0)
    m.command_finished("rm -rf", "api", "failed", 0.5)
    labels = {"command": "compact", "origin": "agent", "result": "completed"}
    assert sample(m, "ccw_commands_total", **labels) == 1
    assert sample(m, "ccw_commands_total", command="other", origin="api", result="failed") == 1
    assert sample(m, "ccw_command_duration_seconds_count", command="compact") == 1
    assert sample(m, "ccw_command_duration_seconds_count", command="other") == 1


def test_compaction_metric_bounded():
    m = Metrics()
    m.compaction("manual")
    m.compaction("auto")
    m.compaction("weird")
    m.compaction("unknown")
    assert sample(m, "ccw_compactions_total", trigger="manual") == 1
    assert sample(m, "ccw_compactions_total", trigger="auto") == 1
    assert sample(m, "ccw_compactions_total", trigger="unknown") == 2
