import re
from pathlib import Path

import pytest
import yaml

from claude_code_wrapper.config import (
    DEFAULT_COMMAND_DENY,
    DEFAULT_DIALOG_PATTERNS,
    load_config,
)

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()
EXAMPLES = ROOT / "src" / "claude_code_wrapper" / "data" / "examples"


@pytest.fixture(autouse=True)
def stub_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GIT_USER_NAME", "Test User")
    monkeypatch.setenv("GIT_USER_EMAIL", "test@example.com")


def _section(heading: str, level: str = "##") -> str:
    match = re.search(
        rf"^{level} {re.escape(heading)}\n(.*?)(?=^{level[:2]} |\Z)", README, re.S | re.M
    )
    assert match, heading
    return match.group(1)


def test_readme_slash_commands_subsections():
    assert README.index("\n## Hook examples\n") < README.index("\n## Slash commands\n")
    body = _section("Slash commands", "##")
    heads = re.findall(r"^### (.+)$", body, re.M)
    assert heads == [
        "The ccw command tool",
        "Commands endpoint",
        "Lifecycle",
        "Configuration",
        "Idle auto-compact",
        "Limits",
    ]


def test_readme_slash_commands_content():
    body = _section("Slash commands")
    for needle in (
        "mcp__ccw__command",
        "${CCW_HOOK_TOKEN}",
        "POST /v1/sessions/{id}/commands",
        "turn_deferred",
        "allow_force",
        "min_context_tokens",
        "PostCompact",
        "compacted",
        "sessions.commands",
        "sessions.auto_compact",
        "turn_max_seconds",
        "--force",
        "sessions.commands.deny",
        "command_denied",
        "dialog_grace_seconds",
        "dialog_patterns",
        "dismissed",
        "dialog_stuck",
        "UserPromptSubmit",
        "claude.boot.timeout_seconds",
    ):
        assert needle in body, needle
    assert "the five names above are the whole set" not in README
    assert "are not supported" not in body


def test_readme_limits_describe_classes_and_bounds():
    limits = _section("Limits", "###")
    for needle in (
        "any command name",
        "`sessions.commands.deny`",
        "`*`",
        "dialog",
        "`dismissed`",
        "`dialog_stuck`",
        "prompt",
        "result `turn`",
        "20000",
        "`timeout_seconds`",
        "1.5 s",
        "`claude.boot.timeout_seconds`",
        "twice",
        "`sessions.max_restarts`",
        "4 commands",
        "`limits.turn_max_seconds`",
    ):
        assert needle in limits, needle
    lifecycle = _section("Lifecycle", "###")
    assert "twice" in lifecycle


def test_hook_examples_mention_auto_compact():
    hooks = _section("Hook examples")
    tatara = hooks.split("### magic-trainer")[0]
    mtg = hooks.split("### magic-trainer")[1]
    assert "auto_compact" in tatara
    assert "auto_compact" in mtg


def test_config_example_documents_command_keys():
    text = (ROOT / "config.example.yaml").read_text()
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("sessions:"))
    end = next(
        i
        for i in range(start + 1, len(lines))
        if lines[i] and not lines[i][0].isspace() and lines[i][0] != "#"
    )
    block = "\n".join(lines[start:end])
    assert "# commands:" in block and "# auto_compact:" in block
    commented = re.compile(r"^(\s*)# (\s*[a-z_]+:.*)$")
    uncommented = "\n".join(commented.sub(r"\1\2", ln) for ln in lines[start:end])
    sessions = yaml.safe_load(uncommented)["sessions"]
    cfg = load_config(ROOT / "config.example.yaml").sessions
    assert sessions["commands"]["agent"] == cfg.commands.agent == []
    assert sessions["commands"]["client"] == cfg.commands.client == []
    assert sessions["commands"]["timeout_seconds"] == cfg.commands.timeout_seconds
    assert sessions["commands"]["continue_prompt"] == cfg.commands.continue_prompt
    assert sessions["commands"]["allow_force"] is cfg.commands.allow_force is False
    assert sessions["commands"]["quiet_seconds"] == cfg.commands.quiet_seconds
    assert sessions["commands"]["deny"] == cfg.commands.deny == list(DEFAULT_COMMAND_DENY)
    assert sessions["commands"]["dialog_grace_seconds"] == cfg.commands.dialog_grace_seconds == 2.0
    assert (
        sessions["commands"]["dialog_patterns"]
        == cfg.commands.dialog_patterns
        == list(DEFAULT_DIALOG_PATTERNS)
    )
    assert sessions["auto_compact"] == {
        "idle_seconds": 0,
        "instructions": "",
        "min_context_tokens": 0,
    }


def test_workbench_example_enables_commands():
    cfg = load_config(EXAMPLES / "workbench.config.yaml").sessions
    assert cfg.commands.agent == ["compact", "reload-plugins"]
    assert cfg.commands.client == ["*"]
    assert cfg.auto_compact.idle_seconds == 1800
    assert cfg.auto_compact.instructions


@pytest.mark.parametrize("name", ["tatara", "magic-trainer"])
def test_project_examples_auto_compact(name):
    cfg = load_config(EXAMPLES / f"{name}.config.yaml").sessions
    assert cfg.commands.agent == ["compact", "reload-plugins"]
    assert cfg.auto_compact.idle_seconds == 1800
    assert cfg.auto_compact.instructions
    assert "\n" not in cfg.auto_compact.instructions
