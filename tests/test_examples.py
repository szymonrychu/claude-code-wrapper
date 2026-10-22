from pathlib import Path

import pytest

from claude_code_wrapper.config import load_config

ROOT = Path(__file__).resolve().parent.parent
CONFIGS = [ROOT / "config.example.yaml", *sorted((ROOT / "examples").glob("*.yaml"))]
STUB_ENV = {
    "CLAUDE_CODE_OAUTH_TOKEN": "tok",
    "GIT_USER_NAME": "Test User",
    "GIT_USER_EMAIL": "test@example.com",
    "CCW_AUTH_TOKEN": "auth-token-0123456789",
    "CCW_PEER_TOKEN": "peer-token-abcdefghij",
    "CCW_PEER_NAME": "a",
    "CCW_PEER_URL": "http://ccw-a:8080",
    "CCW_PEER_SEED": "http://ccw-b:8080",
}


@pytest.fixture
def stub_env(monkeypatch):
    for k, v in STUB_ENV.items():
        monkeypatch.setenv(k, v)


WORKBENCH = ROOT / "examples" / "workbench.config.yaml"


@pytest.mark.parametrize("path", CONFIGS, ids=lambda p: p.name)
def test_every_example_config_loads(path, stub_env):
    assert load_config(path) is not None


def test_workbench_example_plugin(stub_env):
    cfg = load_config(WORKBENCH)
    assert [p.plugin for p in cfg.plugins.install] == ["workbench@szymonrychu-workbench"]
    assert cfg.plugins.install[0].required
    (market,) = cfg.plugins.marketplaces
    assert market.name == "szymonrychu-workbench"
    assert market.source.startswith("/")
    assert "superpowers@claude-plugins-official" in cfg.plugins.disable


def test_workbench_example_workdir_and_questions(stub_env):
    cfg = load_config(WORKBENCH)
    assert cfg.init.workdir.mode == "mounted"
    assert cfg.init.workdir.path == "/workdir/workbench"
    assert cfg.sessions.ask_user_question == "forward"
    assert cfg.init.git.user_name == "Test User"
    assert cfg.claude.permission_mode == "bypassPermissions"
    assert cfg.claude.env.extra["WORKBENCH_ROOT"] == "/workdir/workbench"
    (hook,) = cfg.hooks.onAgentToolCall
    assert hook.matcher == "Bash"


def test_workbench_example_rules_copied(stub_env):
    cfg = load_config(WORKBENCH)
    assert [(f.source, f.target) for f in cfg.claude.config_files] == [
        ("/opt/plugins/claude-infra-plugin/rules", "rules")
    ]
