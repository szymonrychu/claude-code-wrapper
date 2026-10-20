import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from claude_code_wrapper.config import (
    HOOK_EVENTS,
    TOOL_EVENTS,
    VETO_EVENTS,
    CommandAction,
    Config,
    ConfigError,
    WebhookAction,
    load_config,
)

ROOT = Path(__file__).resolve().parent.parent


def write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


def err(tmp_path, text, env=None):
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, text))
    return str(exc.value)


def test_minimal_config_defaults(tmp_path):
    cfg = load_config(write(tmp_path, ""))
    assert cfg.server.port == 8080 and cfg.server.internal_port == 8090
    assert cfg.paths.workdir_root == "/workdir"
    assert cfg.claude.permission_mode == "bypassPermissions"
    assert cfg.claude.permissions.deny == ["EnterPlanMode", "ExitPlanMode"]
    assert cfg.claude.boot.timeout_seconds == 60
    assert cfg.claude.boot.dialogs[0].pattern == "Bypass Permissions mode"
    assert cfg.claude.boot.dialogs[0].keys == ["\x1b[B", "\r"]
    assert cfg.claude.boot.dialogs[0].delay_seconds == 0.6
    assert "? for shortcuts" in cfg.claude.boot.ready_markers
    assert "API Error: 401" in cfg.claude.boot.fatal_patterns
    assert cfg.init.workdir.mode == "empty"
    assert cfg.mise.trusted_paths == ["/workdir"]
    assert cfg.sessions.max_sessions == 4 and cfg.sessions.ask_user_question == "forward"
    assert cfg.limits.sse_replay == 100
    assert cfg.hooks.onCreate == []


def test_event_constants():
    assert len(HOOK_EVENTS) == 12 and HOOK_EVENTS[0] == "onCreate" and HOOK_EVENTS[-1] == "onError"
    assert VETO_EVENTS == ("onCreate", "onResume", "onTurn", "onAgentToolCall")
    assert TOOL_EVENTS == ("onAgentToolCall", "onToolResult")


def test_unknown_key_rejected_with_path(tmp_path):
    msg = err(tmp_path, "server: {bogus: 1}\nnope: 1\n")
    assert "server.bogus:" in msg and "nope:" in msg


def test_all_errors_listed(tmp_path):
    msg = err(tmp_path, "server: {port: 9000, internal_port: 9000}\nsessions: {max_sessions: 0}\n")
    assert "server.internal_port:" in msg
    assert "sessions.max_sessions:" in msg
    assert len(msg.strip().splitlines()) >= 2


def test_ports_zero_ok(tmp_path):
    cfg = load_config(write(tmp_path, "server: {port: 0, internal_port: 0}\n"))
    assert cfg.server.port == 0


def test_paths_absolute(tmp_path):
    assert "paths.workdir_root:" in err(tmp_path, "paths: {workdir_root: rel}\n")


def test_workdir_mode_rules(tmp_path):
    assert "init.workdir.path:" in err(tmp_path, "init: {workdir: {mode: mounted}}\n")
    assert "init.workdir.path:" in err(tmp_path, "init: {workdir: {mode: mounted, path: rel}}\n")
    assert "init.workdir.repo_url:" in err(tmp_path, "init: {workdir: {mode: clone}}\n")
    cfg = load_config(write(tmp_path, "init: {workdir: {mode: mounted, path: /src}}\n"))
    assert cfg.init.workdir.path == "/src"


def test_setup_commands_forms(tmp_path):
    cfg = load_config(
        write(tmp_path, "init: {setup_commands: ['echo a', {run: 'echo b', timeout_seconds: 5}]}\n")
    )
    a, b = cfg.init.setup_commands
    assert (a.run, a.timeout_seconds) == ("echo a", 600)
    assert (b.run, b.timeout_seconds) == ("echo b", 5)


def test_dangerous_flag_rejected(tmp_path):
    msg = err(tmp_path, "claude: {extra_args: ['--dangerously-skip-permissions']}\n")
    assert "claude.extra_args:" in msg


def test_webhook_and_command_action_validation(tmp_path):
    cfg = load_config(
        write(
            tmp_path,
            """
hooks:
  onCreate:
    - {type: command, run: echo hi}
  onAgentToolCall:
    - {type: webhook, url: https://x.test/h, secret: s3cr3tvalue, matcher: "Bash|Edit"}
""",
        )
    )
    c = cfg.hooks.onCreate[0]
    assert isinstance(c, CommandAction)
    assert (c.timeout_seconds, c.blocking, c.on_error) == (30, False, "allow")
    w = cfg.hooks.onAgentToolCall[0]
    assert isinstance(w, WebhookAction)
    assert (w.timeout_seconds, w.retries, w.blocking, w.on_error) == (10, 3, False, "allow")


def test_webhook_url_scheme(tmp_path):
    msg = err(tmp_path, "hooks: {onTurn: [{type: webhook, url: 'ftp://x'}]}\n")
    assert "hooks.onTurn.0.webhook.url:" in msg


def test_matcher_only_on_tool_events(tmp_path):
    msg = err(tmp_path, "hooks: {onTurn: [{type: command, run: x, matcher: Bash}]}\n")
    assert "hooks.onTurn.0.command.matcher:" in msg
    msg = err(tmp_path, "hooks: {onToolResult: [{type: command, run: x, matcher: '('}]}\n")
    assert "hooks.onToolResult.0.command.matcher:" in msg


def test_hook_event_names_validated(tmp_path):
    assert "hooks.onBogus:" in err(tmp_path, "hooks: {onBogus: []}\n")


def test_template_placeholders_declared(tmp_path):
    ok = """
prompts:
  initial_prompt: {template: greet, variables: {who: bob}}
  templates:
    greet: {text: "hi {{who}}", variables: {who: {required: true}}}
"""
    cfg = load_config(write(tmp_path, ok))
    assert cfg.prompts.initial_prompt.template == "greet"
    msg = err(tmp_path, "prompts: {templates: {t: {text: 'hi {{who}}'}}}\n")
    assert "prompts.templates.t.text:" in msg and "who" in msg


def test_initial_prompt_string(tmp_path):
    cfg = load_config(write(tmp_path, "prompts: {initial_prompt: hello}\n"))
    assert cfg.prompts.initial_prompt == "hello"


def test_plugin_ids_validated(tmp_path):
    ok = """
plugins:
  marketplaces: [{name: mine, source: github:me/m}]
  install: [{plugin: a@mine}, {plugin: b@claude-plugins-official, scope: project, required: false}]
"""
    cfg = load_config(write(tmp_path, ok))
    assert cfg.plugins.install[0].scope == "user" and cfg.plugins.install[0].required
    assert "plugins.install.0.plugin:" in err(
        tmp_path, "plugins: {install: [{plugin: nomarket}]}\n"
    )
    assert "plugins.install.0.plugin:" in err(
        tmp_path, "plugins: {install: [{plugin: a@unknown}]}\n"
    )


def test_ask_user_question(tmp_path):
    assert "sessions.ask_user_question:" in err(tmp_path, "sessions: {ask_user_question: maybe}\n")


def test_config_files_targets(tmp_path):
    for t in ("/abs", "../x", "a/../../x"):
        msg = err(tmp_path, f"claude: {{config_files: [{{source: /a, target: '{t}'}}]}}\n")
        assert "claude.config_files.0.target:" in msg
    cfg = load_config(write(tmp_path, "claude: {config_files: [{source: /a, target: sub/x}]}\n"))
    assert cfg.claude.config_files[0].target == "sub/x"


def test_mcp_variants(tmp_path):
    cfg = load_config(
        write(
            tmp_path,
            """
claude:
  mcp_servers:
    a: {type: stdio, command: foo, args: [x], env: {API_TOKEN: abc}}
    b: {type: http, url: 'https://h', headers: {Authorization: Bearer zzz}}
    c: {type: sse, url: 'https://s'}
""",
        )
    )
    assert cfg.claude.mcp_servers["a"].command == "foo"
    assert err(tmp_path, "claude: {mcp_servers: {a: {type: stdio}}}\n")


def test_interpolation_in_load(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_PORT_TOKEN", "tok")
    cfg = load_config(write(tmp_path, "server: {auth_token: '${MY_PORT_TOKEN}'}\n"))
    assert cfg.server.auth_token == "tok"
    msg = err(tmp_path, "server: {auth_token: '${DEFINITELY_UNSET_VAR_X}'}\n")
    assert "server.auth_token" in msg and "DEFINITELY_UNSET_VAR_X" in msg


def test_bad_yaml(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "a: [\n"))
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.yaml")


def test_redacted_masks_secrets(tmp_path, monkeypatch):
    cfg = load_config(
        write(
            tmp_path,
            """
server: {auth_token: tok-secret-1, callback_secret: cb-secret-1}
claude:
  env: {claude_code_oauth_token: sk-ant-oat-verysecret, extra: {MY_SECRET: hush}}
  mcp_servers:
    a: {type: stdio, command: foo, env: {API_TOKEN: envsecret, PLAIN: visible}}
    b: {type: http, url: 'https://h', headers: {Authorization: Bearer zzz}}
hooks:
  onTurn: [{type: webhook, url: 'https://x', secret: whsecret, headers: {X: y}}]
""",
        )
    )
    red = cfg.redacted()
    dumped = json.dumps(red)
    for s in (
        "tok-secret-1",
        "cb-secret-1",
        "verysecret",
        "hush",
        "envsecret",
        "Bearer zzz",
        "whsecret",
    ):
        assert s not in dumped
    assert red["server"]["auth_token"] == "***"
    assert red["claude"]["mcp_servers"]["a"]["env"]["PLAIN"] == "visible"
    assert red["claude"]["mcp_servers"]["b"]["headers"]["Authorization"] == "***"
    assert red["hooks"]["onTurn"][0]["secret"] == "***"
    assert Config().redacted()["server"]["auth_token"] == ""


def test_full_example_loads(monkeypatch):
    for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "CCW_AUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_config(ROOT / "config.example.yaml")
    text = (ROOT / "config.example.yaml").read_text()
    for ev in HOOK_EVENTS:
        assert ev in text
    for section in (
        "server",
        "paths",
        "claude",
        "init",
        "prompts",
        "mise",
        "plugins",
        "hooks",
        "sessions",
        "limits",
    ):
        assert f"\n{section}:" in "\n" + text
    assert cfg.server.auth_token == ""
    TypeAdapter(dict).validate_python(cfg.redacted())


def test_claude_env_section_typed(tmp_path):
    from claude_code_wrapper.claude_env import ClaudeEnv

    text = "claude:\n  env:\n    claude_code_disable_nonessential_traffic: true\n"
    cfg = load_config(write(tmp_path, text))
    assert isinstance(cfg.claude.env, ClaudeEnv)
    env = cfg.claude.env.to_env()
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert "claude.env" in err(tmp_path, "claude:\n  env:\n    not_a_var: 1\n")


def test_empty_auth_vars_are_unset(monkeypatch):
    for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "CCW_AUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_config(ROOT / "config.example.yaml")
    assert cfg.claude.env.to_env() == {}
    assert cfg.claude.env.redacted() == {}


def test_hook_events_match_hooks_config_fields():
    from claude_code_wrapper.config import HOOK_EVENTS, HooksConfig

    assert tuple(HooksConfig.model_fields) == HOOK_EVENTS
