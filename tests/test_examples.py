from pathlib import Path

import pytest

from claude_code_wrapper.config import load_config

ROOT = Path(__file__).resolve().parent.parent
CONFIGS = [ROOT / "config.example.yaml", *sorted((ROOT / "examples").glob("*.yaml"))]
STUB_ENV = {
    "CLAUDE_CODE_OAUTH_TOKEN": "tok",
    "GIT_USER_NAME": "Test User",
    "GIT_USER_EMAIL": "test@example.com",
}


@pytest.fixture
def stub_env(monkeypatch):
    for k, v in STUB_ENV.items():
        monkeypatch.setenv(k, v)


@pytest.mark.parametrize("path", CONFIGS, ids=lambda p: p.name)
def test_example_config_loads(path, stub_env):
    assert load_config(path) is not None


def test_workbench_config(stub_env):
    cfg = load_config(ROOT / "examples" / "workbench.config.yaml")
    assert [p.plugin for p in cfg.plugins.install] == ["workbench@szymonrychu-workbench"]
    assert cfg.plugins.install[0].required
    (market,) = cfg.plugins.marketplaces
    assert market.name == "szymonrychu-workbench"
    assert market.source.startswith("/")
    assert "superpowers@claude-plugins-official" in cfg.plugins.disable
    assert cfg.init.workdir.mode == "mounted"
    assert cfg.init.workdir.path == "/workdir/workbench"
    assert cfg.sessions.ask_user_question == "forward"
    assert [(f.source, f.target) for f in cfg.claude.config_files] == [
        ("/opt/plugins/claude-infra-plugin/rules", "rules")
    ]
    assert cfg.init.git.user_name == "Test User"
    assert cfg.claude.permission_mode == "bypassPermissions"
    assert cfg.claude.env.extra["WORKBENCH_ROOT"] == "/workdir/workbench"
    (hook,) = cfg.hooks.onAgentToolCall
    assert hook.matcher == "Bash"
