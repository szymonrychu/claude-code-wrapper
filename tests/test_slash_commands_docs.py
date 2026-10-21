import re
from pathlib import Path

import pytest
import yaml

from claude_code_wrapper.config import load_config

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()
EXAMPLES = ROOT / "src" / "claude_code_wrapper" / "data" / "examples"
ALL_COMMANDS = ["compact", "reload-plugins", "reload-skills", "mcp", "context"]


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


def test_readme_slash_commands_section_follows_hook_examples():
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
    ):
        assert needle in body, needle


def test_hook_examples_mention_auto_compact():
    hooks = _section("Hook examples")
    tatara = hooks.split("### magic-trainer")[0]
    mtg = hooks.split("### magic-trainer")[1]
    assert "auto_compact" in tatara
    assert "auto_compact" in mtg


def test_config_example_documents_commands_and_auto_compact_at_defaults():
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
    assert sessions["auto_compact"] == {
        "idle_seconds": 0,
        "instructions": "",
        "min_context_tokens": 0,
    }


def test_workbench_example_enables_commands_and_auto_compact():
    cfg = load_config(EXAMPLES / "workbench.config.yaml").sessions
    assert cfg.commands.agent == ["compact", "reload-plugins"]
    assert sorted(cfg.commands.client) == sorted(ALL_COMMANDS)
    assert cfg.auto_compact.idle_seconds == 1800
    assert cfg.auto_compact.instructions


@pytest.mark.parametrize("name", ["tatara", "magic-trainer"])
def test_project_examples_enable_agent_commands_and_auto_compact(name):
    cfg = load_config(EXAMPLES / f"{name}.config.yaml").sessions
    assert cfg.commands.agent == ["compact", "reload-plugins"]
    assert cfg.auto_compact.idle_seconds == 1800
    assert cfg.auto_compact.instructions
    assert "\n" not in cfg.auto_compact.instructions
