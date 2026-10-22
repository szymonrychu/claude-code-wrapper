"""Command and compaction vocabulary: hook events, veto decisions, metrics."""

from __future__ import annotations

from test_metrics import sample

from claude_code_wrapper.config import HOOK_EVENTS, VETO_EVENTS, HooksConfig
from claude_code_wrapper.hooks import EVENT_DECISIONS
from claude_code_wrapper.metrics import Metrics


def test_hook_events_and_decisions():
    start = HOOK_EVENTS.index("onCommand")
    assert HOOK_EVENTS[start : start + 3] == ("onCommand", "onCommandComplete", "onError")
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


def test_on_command_is_a_veto_event():
    assert "onCommand" in VETO_EVENTS
    assert EVENT_DECISIONS["onCommand"] == ("allow", "deny")
    assert "deny" not in EVENT_DECISIONS["onCommandComplete"]
    cfg = HooksConfig.model_validate({"onCommand": [{"type": "command", "run": "true"}]})
    assert len(cfg.onCommand) == 1


def test_hook_events_include_command_hooks():
    assert len(HOOK_EVENTS) == 20
    start = HOOK_EVENTS.index("onCommand")
    assert HOOK_EVENTS[start : start + 3] == ("onCommand", "onCommandComplete", "onError")
    assert "onCommand" in VETO_EVENTS


def test_command_metric_families_render():
    m = Metrics()
    m.command_finished("compact", "agent", "cancelled", 1.0)
    m.compaction("manual")
    text = m.render().decode()
    for fam in ("ccw_commands_total", "ccw_command_duration_seconds", "ccw_compactions_total"):
        assert fam in text
    assert 'command="compact"' in text and 'origin="agent"' in text and 'result="cancelled"' in text
    assert 'trigger="manual"' in text


def test_metric_labels_bounded():
    m = Metrics()
    m.command_finished("rm -rf", "api", "failed", 0.5)
    m.compaction("weird")
    assert sample(m, "ccw_commands_total", command="other", origin="api", result="failed") == 1
    assert sample(m, "ccw_compactions_total", trigger="unknown") == 1
