import json
import stat
import sys
from pathlib import Path

from claude_code_wrapper.bootstrap.settings import (
    RELAY_MATCHERS,
    PermissionTimeouts,
    RelayTimeouts,
    permission_timeouts,
    relay_timeouts,
    render_settings,
)
from claude_code_wrapper.config import Config
from claude_code_wrapper.hook_relay import CLAUDE_HOOK_EVENTS

RELAY = "/opt/ccw-hook"


def _cfg(**kw):
    return Config.model_validate(kw)


def _render(tmp_path, cfg=None, **kw):
    return render_settings(tmp_path, cfg or Config(), relay_command=RELAY, **kw)


def test_relay_hooks_for_every_event(tmp_path):
    doc = _render(tmp_path)
    for event in CLAUDE_HOOK_EVENTS:
        (group,) = doc["hooks"][event]
        assert group["matcher"] == RELAY_MATCHERS.get(event, "")
        (hook,) = group["hooks"]
        assert hook["type"] == "command"
        assert hook["command"] == f"{RELAY} {event}"
        expected = {"UserPromptSubmit": 15, "PermissionRequest": 1830}.get(event, 30)
        assert hook["timeout"] == expected
    assert json.loads((tmp_path / "settings.json").read_text()) == doc


def test_relay_timeouts_helper():
    assert relay_timeouts(Config()) == RelayTimeouts(30, 14)
    cfg = _cfg(
        hooks={
            "onToolResult": [
                {"type": "command", "run": "x", "blocking": True, "timeout_seconds": 100}
            ],
            "onTurn": [{"type": "command", "run": "x", "blocking": True, "timeout_seconds": 900}],
            "onSubagentStop": [
                {"type": "command", "run": "x", "blocking": False, "timeout_seconds": 900}
            ],
        }
    )
    t = relay_timeouts(cfg)
    assert t.hook_timeout == 105
    assert t.attempt_timeout == 51.5
    doc = _render(tmp_path := Path(__import__("tempfile").mkdtemp()), cfg)
    assert doc["hooks"]["Stop"][0]["hooks"][0]["timeout"] == 105
    assert doc["hooks"]["UserPromptSubmit"][0]["hooks"][0]["timeout"] == 15
    assert tmp_path.exists()


def test_existing_relay_entries_replaced_others_kept(tmp_path):
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "enabledPlugins": {"a@b": True},
                "extraKnownMarketplaces": {"m": {}},
                "hooks": {
                    "Stop": [
                        {"matcher": "", "hooks": [{"type": "command", "command": f"{RELAY} Stop"}]},
                        {"matcher": "x", "hooks": [{"type": "command", "command": "other"}]},
                    ]
                },
            }
        )
    )
    _render(tmp_path)
    doc = _render(tmp_path)
    assert doc["enabledPlugins"] == {"a@b": True}
    assert doc["extraKnownMarketplaces"] == {"m": {}}
    cmds = [h["command"] for g in doc["hooks"]["Stop"] for h in g["hooks"]]
    assert cmds.count(f"{RELAY} Stop") == 1
    assert "other" in cmds


def test_permissions_rendered(tmp_path):
    cfg = _cfg(
        claude={"permission_mode": "plan", "permissions": {"allow": ["Bash"], "deny": ["X"]}},
        sessions={"ask_user_question": "deny"},
    )
    doc = _render(tmp_path, cfg)
    assert doc["permissions"] == {
        "defaultMode": "plan",
        "allow": ["Bash"],
        "deny": ["X", "AskUserQuestion"],
    }
    cfg = _cfg(
        claude={"permissions": {"deny": ["AskUserQuestion"]}},
        sessions={"ask_user_question": "deny"},
    )
    deny = _render(tmp_path, cfg)["permissions"]["deny"]
    assert deny.count("AskUserQuestion") == 1
    assert "AskUserQuestion" not in _render(tmp_path)["permissions"]["deny"]


def test_overlay_merged_hooks_appended(tmp_path):
    doc = _render(tmp_path)
    assert doc["enableAllProjectMcpServers"] is True
    cfg = _cfg(
        plugins={"disable": ["p@m"]},
        claude={
            "settings": {
                "enableAllProjectMcpServers": False,
                "env": {"A": "1"},
                "hooks": {
                    "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "mine"}]}],
                    "Notification": [{"hooks": [{"type": "command", "command": "n"}]}],
                },
            }
        },
    )
    doc = _render(tmp_path, cfg)
    assert doc["enableAllProjectMcpServers"] is False
    assert doc["enabledPlugins"]["p@m"] is False
    assert doc["env"] == {"A": "1", "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1"}
    stop = [h["command"] for g in doc["hooks"]["Stop"] for h in g["hooks"]]
    assert stop == [f"{RELAY} Stop", "mine"]
    assert "Notification" in doc["hooks"]
    again = _render(tmp_path, cfg)
    assert again == doc


def test_idempotent_rerender(tmp_path):
    _render(tmp_path)
    p = tmp_path / "settings.json"
    first = p.read_text()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    _render(tmp_path)
    assert p.read_text() == first


def test_relay_command_resolution(tmp_path):
    doc = render_settings(tmp_path, Config())
    cmd = doc["hooks"]["Stop"][0]["hooks"][0]["command"]
    sibling = Path(sys.executable).parent / "ccw-hook"
    assert cmd == f"{sibling if sibling.exists() else 'ccw-hook'} Stop"


def test_gate_timeouts_from_blocking_hooks(tmp_path):
    cfg = _cfg(
        hooks={
            "onAgentToolCall": [
                {"type": "command", "run": "x", "blocking": True, "timeout_seconds": 60}
            ]
        }
    )
    doc = _render(tmp_path, cfg)
    for event in ("PreToolUse", "PostToolUse"):
        assert doc["hooks"][event][0]["hooks"][0]["timeout"] == 65


def test_ask_user_question_deny_mode(tmp_path):
    cfg = _cfg(
        claude={"permissions": {"deny": ["AskUserQuestion"]}},
        sessions={"ask_user_question": "deny"},
    )
    assert _render(tmp_path, cfg)["permissions"]["deny"] == ["AskUserQuestion"]
    cfg = _cfg(sessions={"ask_user_question": "deny"})
    deny = _render(tmp_path, cfg)["permissions"]["deny"]
    assert deny.count("AskUserQuestion") == 1
    assert "AskUserQuestion" not in _render(tmp_path)["permissions"]["deny"]


def test_disabled_plugins_false(tmp_path):
    cfg = _cfg(plugins={"disable": ["p@m", "q@m"]})
    doc = _render(tmp_path, cfg)
    assert doc["enabledPlugins"] == {"p@m": False, "q@m": False}


def test_enabled_plugins_written_by_cli_preserved(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"enabledPlugins": {"a@b": True}}))
    doc = _render(tmp_path, _cfg(plugins={"disable": ["p@m"]}))
    assert doc["enabledPlugins"] == {"a@b": True, "p@m": False}


def test_auto_mode_seeds_skip_auto_permission_prompt(tmp_path):
    doc = _render(tmp_path, _cfg(claude={"permission_mode": "auto"}))
    assert doc["permissions"]["defaultMode"] == "auto"
    assert doc["skipAutoPermissionPrompt"] is True


def test_bypass_mode_seeds_skip_auto_permission_prompt(tmp_path):
    doc = _render(tmp_path)
    assert doc["permissions"]["defaultMode"] == "bypassPermissions"
    assert doc["skipAutoPermissionPrompt"] is True


def test_permission_timeouts_formula():
    assert permission_timeouts(1800) == PermissionTimeouts(1830, 1815)
    t = permission_timeouts(60)
    assert t.relay_timeout < t.hook_timeout


def test_default_permission_timeout_ties_to_formula():
    from claude_code_wrapper.config import DEFAULT_PERMISSION_HOLD_SECONDS
    from claude_code_wrapper.hook_relay import DEFAULT_PERMISSION_TIMEOUT

    default = permission_timeouts(DEFAULT_PERMISSION_HOLD_SECONDS)
    assert default.relay_timeout == DEFAULT_PERMISSION_TIMEOUT
    assert Config().sessions.commands.permission_hold_seconds == DEFAULT_PERMISSION_HOLD_SECONDS


def test_permission_hook_timeout_follows_override(tmp_path):
    doc = _render(tmp_path, permission_hold_seconds=60)
    assert doc["hooks"]["PermissionRequest"][0]["hooks"][0]["timeout"] == 90


def test_prevention_settings_written(tmp_path):
    doc = _render(tmp_path)
    assert doc["permissions"]["defaultMode"] == "bypassPermissions"
    assert doc["skipDangerousModePermissionPrompt"] is True
    assert doc["skipAutoPermissionPrompt"] is True
    assert doc["feedbackSurveyRate"] == 0
    assert doc["feedbackDrafts"] == "off"
    assert doc["env"]["CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY"] == "1"


def test_prevention_env_merges_and_overlay_wins(tmp_path):
    cfg = _cfg(claude={"settings": {"env": {"FOO": "1"}, "feedbackDrafts": "quiet"}})
    doc = _render(tmp_path, cfg)
    assert doc["env"]["FOO"] == "1"
    assert doc["env"]["CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY"] == "1"
    assert doc["feedbackDrafts"] == "quiet"
