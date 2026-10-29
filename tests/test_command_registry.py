"""Command registry, queue, config and the main-agent guard (design D1-D3)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from claude_code_wrapper.commands import (
    MAX_COMMAND_ARGS,
    CommandQueue,
    CommandRejected,
    effective_commands,
    render,
    validate,
)
from claude_code_wrapper.config import (
    DEFAULT_COMMAND_DENY,
    CommandsConfig,
    CommandsOverride,
    Config,
    ConfigError,
    load_config,
)
from claude_code_wrapper.manager import CreateSessionRequest

ALL = ["compact", "reload-plugins", "reload-skills", "status", "context"]


def cfg(**kw) -> CommandsConfig:
    return CommandsConfig(agent=ALL, client=ALL, **kw)


def rejected(fn, *a, **kw) -> CommandRejected:
    with pytest.raises(CommandRejected) as e:
        fn(*a, **kw)
    return e.value


# ------------------------------------------------------------------ config
def test_commands_config_defaults_off():
    c = Config().sessions.commands
    assert c.agent == [] and c.client == []
    assert c.timeout_seconds == {"compact": 600, "default": 60}
    assert c.continue_prompt and c.allow_force is False and c.quiet_seconds == 1.5


DENY = [
    "exit",
    "quit",
    "clear",
    "reset",
    "new",
    "resume",
    "continue",
    "branch",
    "fork",
    "cd",
    "background",
    "bg",
    "teleport",
    "tp",
    "stop",
    "remote-control",
    "rc",
    "restart",
    "update",
    "login",
    "logout",
    "upgrade",
    "setup-bedrock",
    "setup-vertex",
    "web-setup",
    "design-login",
]
#: Added by the 2026-10-29 user decision; `app` is desktop's alias in claude 2.1.304.
ADDED_DENY = [
    "heapdump",
    "plugin",
    "plugins",
    "marketplace",
    "install",
    "import",
    "daemon",
    "schedule",
    "routines",
    "add-dir",
    "export",
    "install-github-app",
    "install-slack-app",
    "design-consent",
    "design-revoke",
    "desktop",
    "app",
    "remote-env",
    "feedback",
    "bug",
    "extra-usage",
    "usage-credits",
    "terminal-setup",
    "ide",
    "memory",
]
#: Added in 0.4.2: commands that reach the agent account's cloud features (privacy-settings,
#: ultraplan, ultrareview, autofix-pr, cloud-plugins, session and its alias remote) or rewrite the
#: session's tool and permission settings (mcp, permissions and allowed-tools, config and
#: settings, hooks).
ACCOUNT_SETTINGS_DENY = [
    "privacy-settings",
    "ultraplan",
    "ultrareview",
    "autofix-pr",
    "cloud-plugins",
    "session",
    "remote",
    "mcp",
    "permissions",
    "allowed-tools",
    "config",
    "settings",
    "hooks",
]
BAD_NAMES = ["rm -rf", "-x", "/clear", "notes.txt"]


def code_of(q: CommandQueue, name: str, origin: str = "client") -> str | None:
    """The rejection code of queueing `name`, None when it was queued."""
    try:
        q.request(name, "", origin=origin, turn_id=None)
    except CommandRejected as exc:
        return exc.code
    return None


def test_unknown_command_name_rejected_at_config(tmp_path):
    p = tmp_path / "c.yaml"
    for bad in BAD_NAMES:
        p.write_text(f"sessions:\n  commands:\n    agent: [compact, {json.dumps(bad)}]\n")
        with pytest.raises(ConfigError) as e:
            load_config(p)
        assert str(e.value).startswith(f"sessions.commands.agent.1: invalid command name {bad!r}")


def test_unknown_client_and_timeout_key(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        'sessions:\n  commands:\n    client: ["rm -rf"]\n    timeout_seconds: {"notes.txt": 5}\n'
    )
    with pytest.raises(ConfigError) as e:
        load_config(p)
    msg = str(e.value)
    assert "sessions.commands.client.0: invalid command name 'rm -rf'" in msg
    assert "sessions.commands.timeout_seconds.notes.txt: invalid command name 'notes.txt'" in msg
    p.write_text('sessions:\n  commands:\n    timeout_seconds: {"*": 5}\n')
    with pytest.raises(ConfigError, match=r"timeout_seconds\.\*: invalid command name"):
        load_config(p)
    p.write_text("sessions:\n  commands:\n    timeout_seconds: {compact: 0}\n")
    with pytest.raises(ConfigError, match=r"timeout_seconds\.compact: must be greater than 0"):
        load_config(p)
    p.write_text("sessions:\n  commands:\n    timeout_seconds: {model: 5, default: 9}\n")
    assert load_config(p).sessions.commands.timeout_seconds == {"model": 5, "default": 9}


def test_command_names_free_form_and_wildcard(tmp_path):
    long = "a" * 100
    for names in (["_x", long, "workbench:x", "Model"], ["*"]):
        c = CommandsConfig(agent=names, client=names)
        assert c.agent == names and c.client == names
        assert CommandsOverride(agent=names, client=names).client == names
    p = tmp_path / "c.yaml"
    for bad in BAD_NAMES:
        for key in ("agent", "client"):
            p.write_text(f"sessions:\n  commands:\n    {key}: [{json.dumps(bad)}]\n")
            with pytest.raises(ConfigError) as e:
                load_config(p)
            assert str(e.value).startswith(f"sessions.commands.{key}.0: invalid command name")
        with pytest.raises(ValidationError, match="invalid command name"):
            CommandsOverride(client=[bad])
    with pytest.raises(ValidationError, match="invalid command name"):
        CreateSessionRequest.model_validate({"commands": {"agent": ["rm -rf"]}})


def test_deny_list_default_and_override(tmp_path):
    assert len(DENY) == 26 and len(ADDED_DENY) == 25
    assert len(ACCOUNT_SETTINGS_DENY) == 13
    full = DENY + ADDED_DENY + ACCOUNT_SETTINGS_DENY
    assert list(DEFAULT_COMMAND_DENY) == full
    assert len(DEFAULT_COMMAND_DENY) == len(set(full)) == 64
    assert Config().sessions.commands.deny == full
    assert CommandsConfig().deny == full
    # an override only extends: [] and ["exit"] leave every default name, ["foo"] adds foo
    assert effective_commands(CommandsConfig(), CommandsOverride(deny=[])).deny == full
    assert effective_commands(CommandsConfig(), CommandsOverride(deny=["exit"])).deny == full
    assert effective_commands(CommandsConfig(), CommandsOverride(deny=["foo"])).deny == full + [
        "foo"
    ]
    assert effective_commands(CommandsConfig(), CommandsOverride(client=["*"])).deny == full
    req = CreateSessionRequest.model_validate({"commands": {"deny": ["exit", "clear"]}})
    assert req.commands.deny == ["exit", "clear"]
    for bad in ("*", "rm -rf"):
        with pytest.raises(ValidationError, match="invalid command name"):
            CreateSessionRequest.model_validate({"commands": {"deny": ["exit", bad]}})
    p = tmp_path / "c.yaml"
    p.write_text("sessions:\n  commands:\n    deny: [exit]\n")
    assert load_config(p).sessions.commands.deny == ["exit"]
    for bad in ("*", "rm -rf"):
        p.write_text(f"sessions:\n  commands:\n    deny: [exit, {json.dumps(bad)}]\n")
        with pytest.raises(ConfigError) as e:
            load_config(p)
        assert str(e.value).startswith("sessions.commands.deny.1: invalid command name")


def test_deny_list_covers_user_extension():
    q = CommandQueue(CommandsConfig(agent=["*"], client=["*"]))
    for name in ADDED_DENY:
        assert name in DEFAULT_COMMAND_DENY, name
        for variant in (name, name.upper(), name.capitalize(), name.title()):
            for origin in ("agent", "client"):
                assert code_of(q, variant, origin) == "denied", (variant, origin)
    assert q.pending() == []


def test_deny_list_covers_account_and_settings():
    q = CommandQueue(CommandsConfig(agent=["*"], client=["*"]))
    for name in ACCOUNT_SETTINGS_DENY:
        assert name in DEFAULT_COMMAND_DENY, name
        for variant in (name, name.upper(), name.capitalize(), name.title()):
            for origin in ("agent", "client"):
                assert code_of(q, variant, origin) == "denied", (variant, origin)
    assert q.pending() == []


def test_create_override_only_extends_deny():
    base = CommandsConfig(client=["*"])
    for deny in ([], ["exit"], ["EXIT"]):
        out = effective_commands(base, CommandsOverride(deny=deny))
        q = CommandQueue(out)
        assert code_of(q, "clear") == "denied" and code_of(q, "plugin") == "denied", deny
        assert code_of(q, "compact") is None
    assert effective_commands(base, CommandsOverride(deny=None)).deny == list(DEFAULT_COMMAND_DENY)
    # new names are compared case-insensitively, base order kept, the override's new names after
    out = effective_commands(base, CommandsOverride(deny=["FOO", "Clear", "foo", "bar"]))
    assert out.deny == [*DEFAULT_COMMAND_DENY, "FOO", "bar"]
    assert code_of(CommandQueue(out), "foo") == "denied"
    # a base list that is not the default is extended the same way
    small = CommandsConfig(client=["*"], deny=["exit"])
    assert effective_commands(small, CommandsOverride(deny=["EXIT", "x"])).deny == ["exit", "x"]
    req = CreateSessionRequest.model_validate({"commands": {"deny": []}})
    assert req.commands.deny == []
    assert effective_commands(base, req.commands).deny == list(DEFAULT_COMMAND_DENY)


def test_deny_list_covers_aliases_and_session_moving_commands():
    q = CommandQueue(CommandsConfig(agent=["*"], client=["*"]))
    aliases = ["reset", "new", "continue", "quit", "update", "bg", "tp", "rc"]
    moving = ["cd", "stop", "upgrade", "setup-bedrock", "setup-vertex", "web-setup", "design-login"]
    for name in aliases + moving:
        assert name in DEFAULT_COMMAND_DENY, name
        for origin in ("agent", "client"):
            assert code_of(q, name, origin) == "denied", (name, origin)
    for name in ("status", "cost"):
        assert name not in DEFAULT_COMMAND_DENY
        for origin in ("agent", "client"):
            assert code_of(q, name, origin) is None, name
    # only the free names queued, once each: the second origin coalesced
    assert [r.name for r in q.pending()] == ["status", "cost"]


def test_deny_is_case_insensitive():
    q = CommandQueue(CommandsConfig(agent=["*"], client=["*"]))
    for name in ("EXIT", "Clear", "Reset", "ReMoTe-CoNtRoL"):
        e = rejected(q.request, name, "", origin="client", turn_id=None)
        assert e.code == "denied" and name in e.message
    upper = CommandQueue(CommandsConfig(client=["*"], deny=["EXIT"]))
    assert code_of(upper, "exit") == "denied"


def test_deny_checked_before_allowlist():
    named = CommandQueue(CommandsConfig(agent=["exit", "compact"], client=["clear", "compact"]))
    assert code_of(named, "exit", "agent") == "denied"
    assert code_of(named, "clear", "client") == "denied"
    star = CommandQueue(CommandsConfig(agent=["*"], client=["*"]))
    assert code_of(star, "exit", "agent") == "denied"
    assert code_of(star, "clear", "client") == "denied"
    assert star.pending() == []
    rec, new = star.request("context", "", origin="client", turn_id=None)
    assert new and rec.name == "context"
    rec, new = star.request("compact", "", origin="agent", turn_id=None)
    assert new and rec.name == "compact"
    # a free name passes deny, allowlist and validation and is queued
    assert code_of(star, "model") is None
    # a name outside the allowlist is still not_allowed; idle stays compact only
    assert code_of(CommandQueue(CommandsConfig(client=["compact"])), "context") == "not_allowed"
    assert code_of(star, "context", "idle") == "not_allowed"
    assert code_of(star, "exit", "idle") == "not_allowed"


def test_bounds_and_extra_forbid():
    with pytest.raises(ValidationError):
        CommandsConfig(quiet_seconds=-1)
    with pytest.raises(ValidationError):
        CommandsConfig(continue_prompt="")
    with pytest.raises(ValidationError):
        CommandsConfig(nope=1)


# ------------------------------------------------------------------ name and args
def test_validate_name_and_args():
    assert MAX_COMMAND_ARGS == 20000
    for name in ("_x", "a" * 100, "workbench:x", "Model", "reload-plugins"):
        assert validate(name, "", allow_force=False) == ""
    assert validate("compact", "  keep\n\tthe plan\n  ", allow_force=False) == "keep\n\tthe plan"
    assert validate("compact", "a\r\nb\r\n", allow_force=False) == "a\nb"
    assert validate("compact", "x" * MAX_COMMAND_ARGS, allow_force=False) == "x" * 20000
    for name in ("", "a.b", "a/b", "/clear", "-x", "a b", "notes.txt"):
        e = rejected(validate, name, "", allow_force=False)
        assert e.code == "invalid_command", name
    assert "/command [args]" in rejected(validate, "", "x", allow_force=False).message
    for args in (
        "a\x1bb",
        "a\x1b[201~\r/exit",
        "a\u2028b",
        "a\u2029b",
        "a\rb",
        "a\x00b",
        "a\x7fb",
        "a\x85b",
        "x" * (MAX_COMMAND_ARGS + 1),
    ):
        e = rejected(validate, "compact", args, allow_force=True)
        assert e.code == "invalid_command", repr(args[:12])
    q = CommandQueue(CommandsConfig(client=["*"]))
    rec, new = q.request("_x", "one\r\ntwo ", origin="client", turn_id=None)
    assert new and rec.args == "one\ntwo"
    assert rejected(q.request, "", "", origin="client", turn_id=None).code == "invalid_command"
    assert rejected(q.request, "a.b", "", origin="client", turn_id=None).code == "invalid_command"
    assert q.pending() == [rec]


def test_render_name_and_args():
    assert render("compact") == "/compact"
    assert render("compact", "") == "/compact"
    assert render("compact", "keep X") == "/compact keep X"
    assert render("mcp", "reconnect github") == "/mcp reconnect github"
    assert render("reload-plugins", "--force") == "/reload-plugins --force"
    assert render("compact", "keep\nthe plan") == "/compact keep\nthe plan"
    assert render("_x", "a\n\tb") == "/_x a\n\tb"


def test_force_needs_allow_force():
    for args in ("--force", "x --force", "a\n--force", "\t--force\t"):
        e = rejected(validate, "reload-plugins", args, allow_force=False)
        assert e.code == "force_not_allowed", args
        assert "allow_force" in e.message
        assert validate("reload-plugins", args, allow_force=True) == args.strip()
    assert rejected(validate, "Reload-Plugins", "--force", allow_force=False).code == (
        "force_not_allowed"
    )
    assert validate("reload-plugins", "--forced", allow_force=False) == "--forced"
    assert validate("reload-plugins", "", allow_force=False) == ""
    assert validate("compact", "--force", allow_force=False) == "--force"  # reload-plugins only
    q = CommandQueue(cfg())
    e = rejected(q.request, "reload-plugins", "--force", origin="agent", turn_id=None)
    assert e.code == "force_not_allowed" and q.list() == []
    rec, new = CommandQueue(cfg(allow_force=True)).request(
        "reload-plugins", "--force", origin="agent", turn_id=None
    )
    assert new and rec.args == "--force"


def test_coalesce_identical_name_and_args():
    q = CommandQueue(CommandsConfig(agent=["*"], client=["*"]))
    a, new = q.request("model", "a", origin="client", turn_id=None)
    assert new and a.args == "a"
    b, new = q.request("model", "b", origin="client", turn_id=None)
    assert new and b is not a and b.args == "b"
    again, new = q.request("model", " a\r\n", origin="agent", turn_id="t1")
    assert again is a and not new and a.origin == "client"
    bare, new = q.request("model", "", origin="client", turn_id=None)
    assert new and bare not in (a, b)
    assert [(r.name, r.args) for r in q.pending()] == [
        ("model", "a"),
        ("model", "b"),
        ("model", ""),
    ]
    q.finish(a, "completed")
    assert q.request("model", "a", origin="client", turn_id=None)[1]


# ------------------------------------------------------------------ queue
def test_queue_allowlist_per_origin():
    q = CommandQueue(CommandsConfig(agent=["compact"], client=["context"]))
    assert rejected(q.request, "context", "", origin="agent", turn_id="t").code == "not_allowed"
    assert rejected(q.request, "compact", "", origin="client", turn_id="t").code == "not_allowed"
    assert q.request("context", "", origin="client", turn_id=None)[1]
    assert q.request("compact", "", origin="idle", turn_id=None)[1]
    assert rejected(q.request, "context", "", origin="idle", turn_id=None).code == "not_allowed"
    assert CommandQueue(CommandsConfig()).cfg.agent == []


def test_coalesce_cap_and_lifecycle():
    q = CommandQueue(cfg())
    a, new = q.request("compact", "first", origin="agent", turn_id="t1")
    assert new and a.id.startswith("cmd_") and a.state == "queued" and a.created_at
    b, new = q.request("compact", "first", origin="client", turn_id="t1")
    assert b is a and not new and a.args == "first"
    for n in ("reload-plugins", "reload-skills", "context"):
        q.request(n, "", origin="agent", turn_id="t1")
    assert rejected(q.request, "status", "", origin="agent", turn_id="t1").code == "queue_full"
    assert q.next() is a and len(q.pending()) == 4
    q.start(a)
    assert a.state == "running" and a.started_at and q.next() is not a
    q.finish(a, "completed", result="ok", output="x")
    assert a.finished_at and a.result == "ok" and q.get(a.id) is a
    q.finish(a, "failed", error="boom")
    assert a.state == "completed" and a.error is None
    # a finished name can be queued again
    assert q.request("compact", "", origin="agent", turn_id="t2")[1]


def test_cancel_queued_and_history_cap():
    q = CommandQueue(cfg())
    r1, _ = q.request("compact", "", origin="agent", turn_id="t1")
    r2, _ = q.request("context", "", origin="agent", turn_id="t2")
    assert q.cancel_queued("t1") == [r1] and r1.state == "cancelled"
    assert r2.state == "queued"
    q.cancel_queued()
    assert r2.state == "cancelled"
    for _ in range(120):
        r, _ = q.request("context", "", origin="agent", turn_id=None)
        q.finish(r, "completed")
    items = q.list()
    assert len(items) == 100 and items[-1] is r


def test_effective_commands():
    base = CommandsConfig(agent=["compact"], allow_force=True)
    out = effective_commands(base, CommandsOverride(client=["context"], quiet_seconds=0))
    assert out.agent == ["compact"] and out.client == ["context"]
    assert out.quiet_seconds == 0 and out.allow_force is True
    assert effective_commands(base, None) is base


def test_create_request_override_validates():
    req = CreateSessionRequest.model_validate({"commands": {"agent": ["compact"]}})
    assert req.commands.agent == ["compact"]
    with pytest.raises(ValidationError, match="invalid command name '/clear'"):
        CreateSessionRequest.model_validate({"commands": {"agent": ["/clear"]}})
    with pytest.raises(ValidationError):
        CreateSessionRequest.model_validate({"commands": {"bogus": 1}})


def test_duplicate_queued_returns_existing():
    q = CommandQueue(cfg())
    a, new = q.request("compact", "first", origin="agent", turn_id="t1")
    assert new
    b, new = q.request("compact", "first", origin="client", turn_id="t1")
    assert b is a and not new and a.args == "first"
    assert len(q.pending()) == 1


def test_queue_cap_and_terminal_records():
    q = CommandQueue(cfg())
    first, _ = q.request("compact", "", origin="agent", turn_id="t")
    for n in ("reload-plugins", "reload-skills", "context"):
        q.request(n, "", origin="agent", turn_id="t")
    assert rejected(q.request, "status", "", origin="agent", turn_id="t").code == "queue_full"
    q.start(first)
    q.finish(first, "completed", result="ok")
    q.finish(first, "failed", error="boom")
    q.cancel_queued()
    assert first.state == "completed" and first.error is None and first.result == "ok"


async def test_create_override_persisted_and_resumed(world) -> None:
    from conftest import wait_turn

    m = world.manager(world.cfg(sessions={"commands": {"agent": ["compact"], "allow_force": True}}))
    snap = await m.create(
        CreateSessionRequest(prompt="hi", commands={"client": ["context"], "allow_force": False})
    )
    sid = snap["session_id"]
    await wait_turn(m, sid)
    cmds = m.router(sid).commands_cfg
    assert cmds.agent == ["compact"] and cmds.client == ["context"] and cmds.allow_force is False
    assert world.meta(sid)["commands"] == {"client": ["context"], "allow_force": False}
    await m.destroy(sid)
    await m.resume(sid)
    cmds = m.router(sid).commands_cfg
    assert cmds.agent == ["compact"] and cmds.client == ["context"] and cmds.allow_force is False


async def test_subagent_ccw_tool_call_refused(tmp_path):
    from test_router import Harness, webhook

    h = Harness(tmp_path, hooks={"onAgentToolCall": [webhook()]})
    r = await h.router.handle_claude_hook(
        "PreToolUse",
        {
            "tool_use_id": "tu9",
            "tool_name": "mcp__ccw__command",
            "tool_input": {"name": "compact"},
            "agent_id": "ag1",
            "agent_type": "Explore",
        },
    )
    out = json.loads(r["stdout"])["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "main agent only" in out["permissionDecisionReason"]
    (ev,) = h.events("tool_denied")
    assert ev.data["agent_id"] == "ag1" and ev.data["tool_name"] == "mcp__ccw__command"
    assert h.hook_posts == [] and not h.events("tool_call")
    r = await h.router.handle_claude_hook(
        "PreToolUse", {"tool_use_id": "t", "tool_name": "mcp__ccw__command", "tool_input": {}}
    )
    assert r["stdout"] == "" and h.events("tool_call")
    await h.router.close()
