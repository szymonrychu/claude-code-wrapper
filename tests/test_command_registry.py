"""Command registry, queue, config and the main-agent guard (design D1-D3)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from claude_code_wrapper.commands import (
    BUILTIN_COMMANDS,
    CommandQueue,
    CommandRejected,
    effective_commands,
    params_schema,
    render,
    validate_params,
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

ALL = ["compact", "reload-plugins", "reload-skills", "mcp", "context"]


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
BAD_NAMES = ["rm -rf", "-x", "/clear", "notes.txt"]


def code_of(q: CommandQueue, name: str, origin: str = "client") -> str | None:
    """The rejection code of queueing `name`, None when it was queued."""
    try:
        q.request(name, {}, origin=origin, turn_id=None)
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
    assert len(DENY) == 26
    assert list(DEFAULT_COMMAND_DENY) == DENY
    assert Config().sessions.commands.deny == DENY
    assert CommandsConfig().deny == DENY
    out = effective_commands(CommandsConfig(), CommandsOverride(deny=["exit"]))
    assert out.deny == ["exit"]
    assert code_of(CommandQueue(out.model_copy(update={"client": ["*"]})), "clear") != "denied"
    assert effective_commands(CommandsConfig(), CommandsOverride(client=["*"])).deny == DENY
    req = CreateSessionRequest.model_validate({"commands": {"deny": ["exit", "clear"]}})
    assert req.commands.deny == ["exit", "clear"]
    p = tmp_path / "c.yaml"
    p.write_text("sessions:\n  commands:\n    deny: [exit]\n")
    assert load_config(p).sessions.commands.deny == ["exit"]
    for bad in ("*", "rm -rf"):
        p.write_text(f"sessions:\n  commands:\n    deny: [exit, {json.dumps(bad)}]\n")
        with pytest.raises(ConfigError) as e:
            load_config(p)
        assert str(e.value).startswith("sessions.commands.deny.1: invalid command name")


def test_deny_list_covers_aliases_and_session_moving_commands():
    q = CommandQueue(CommandsConfig(agent=["*"], client=["*"]))
    aliases = ["reset", "new", "continue", "quit", "update", "bg", "tp", "rc"]
    moving = ["cd", "stop", "upgrade", "setup-bedrock", "setup-vertex", "web-setup", "design-login"]
    for name in aliases + moving:
        assert name in DEFAULT_COMMAND_DENY, name
        for origin in ("agent", "client"):
            assert code_of(q, name, origin) == "denied", (name, origin)
    for name in ("session", "remote"):
        assert name not in DEFAULT_COMMAND_DENY
        for origin in ("agent", "client"):
            assert code_of(q, name, origin) not in ("denied", "not_allowed"), name
    assert q.pending() == []


def test_deny_is_case_insensitive():
    q = CommandQueue(CommandsConfig(agent=["*"], client=["*"]))
    for name in ("EXIT", "Clear", "Reset", "ReMoTe-CoNtRoL"):
        e = rejected(q.request, name, {}, origin="client", turn_id=None)
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
    rec, new = star.request("context", {}, origin="client", turn_id=None)
    assert new and rec.name == "context"
    rec, new = star.request("compact", {}, origin="agent", turn_id=None)
    assert new and rec.name == "compact"
    # a free name passes deny and allowlist and reaches the registry
    assert code_of(star, "model") == "unknown_command"
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


# ------------------------------------------------------------------ params
def test_validate_params():
    assert validate_params("compact", None, allow_force=False) == {}
    assert validate_params("compact", {"instructions": "keep X"}, allow_force=False)
    assert (
        "takes no params"
        in rejected(validate_params, "context", {"a": 1}, allow_force=False).message
    )
    e = rejected(validate_params, "compact", {"x": 1}, allow_force=False)
    assert e.code == "invalid_params" and "instructions" in e.message
    assert (
        rejected(validate_params, "compact", {"instructions": 3}, allow_force=False).code
        == "invalid_params"
    )
    assert (
        rejected(validate_params, "reload-plugins", {"force": "y"}, allow_force=True).code
        == "invalid_params"
    )
    assert (
        rejected(validate_params, "reload-plugins", {"force": True}, allow_force=False).code
        == "force_not_allowed"
    )
    assert validate_params("reload-plugins", {"force": True}, allow_force=True) == {"force": True}
    assert validate_params("reload-plugins", {"force": False}, allow_force=False)
    assert rejected(validate_params, "mcp", {}, allow_force=False).code == "invalid_params"
    assert (
        rejected(validate_params, "mcp", {"action": "list"}, allow_force=False).code
        == "invalid_params"
    )
    for bad in ("a\nb", "a\x1bb", "a\rb", "x" * 2001):
        assert (
            rejected(validate_params, "compact", {"instructions": bad}, allow_force=False).code
            == "invalid_params"
        )
    validate_params("compact", {"instructions": "x" * 2000}, allow_force=False)


def test_render_command_lines():
    assert render("compact", {}) == "/compact"
    assert render("compact", {"instructions": "keep X"}) == "/compact keep X"
    assert render("reload-plugins", {}) == "/reload-plugins"
    assert render("reload-plugins", {"force": True}) == "/reload-plugins --force"
    assert render("reload-skills", {}) == "/reload-skills"
    assert render("mcp", {"action": "reconnect"}) == "/mcp reconnect all"
    assert render("mcp", {"action": "reconnect", "server": "gh"}) == "/mcp reconnect gh"
    assert render("context", {}) == "/context"


def test_params_schema_lists_allowed_commands():
    s = params_schema(["compact", "mcp"])
    assert s["properties"]["name"]["enum"] == ["compact", "mcp"]
    assert "oneOf" not in s
    json.dumps(s)
    assert set(BUILTIN_COMMANDS) == set(ALL)


# ------------------------------------------------------------------ queue
def test_queue_allowlist_per_origin():
    q = CommandQueue(CommandsConfig(agent=["compact"], client=["context"]))
    assert rejected(q.request, "context", {}, origin="agent", turn_id="t").code == "not_allowed"
    assert rejected(q.request, "compact", {}, origin="client", turn_id="t").code == "not_allowed"
    assert q.request("context", {}, origin="client", turn_id=None)[1]
    assert q.request("compact", {}, origin="idle", turn_id=None)[1]
    assert rejected(q.request, "context", {}, origin="idle", turn_id=None).code == "not_allowed"
    assert CommandQueue(CommandsConfig()).cfg.agent == []


def test_coalesce_cap_and_lifecycle():
    q = CommandQueue(cfg())
    a, new = q.request("compact", {"instructions": "first"}, origin="agent", turn_id="t1")
    assert new and a.id.startswith("cmd_") and a.state == "queued" and a.created_at
    b, new = q.request("compact", {"instructions": "second"}, origin="client", turn_id="t1")
    assert b is a and not new and a.args == {"instructions": "first"}
    for n in ("reload-plugins", "reload-skills", "context"):
        q.request(n, {}, origin="agent", turn_id="t1")
    assert (
        rejected(q.request, "mcp", {"action": "reconnect"}, origin="agent", turn_id="t1").code
        == "queue_full"
    )
    assert q.next() is a and len(q.pending()) == 4
    q.start(a)
    assert a.state == "running" and a.started_at and q.next() is not a
    q.finish(a, "completed", result="ok", output="x")
    assert a.finished_at and a.result == "ok" and q.get(a.id) is a
    q.finish(a, "failed", error="boom")
    assert a.state == "completed" and a.error is None
    # a finished name can be queued again
    assert q.request("compact", {}, origin="agent", turn_id="t2")[1]


def test_cancel_queued_and_history_cap():
    q = CommandQueue(cfg())
    r1, _ = q.request("compact", {}, origin="agent", turn_id="t1")
    r2, _ = q.request("context", {}, origin="agent", turn_id="t2")
    assert q.cancel_queued("t1") == [r1] and r1.state == "cancelled"
    assert r2.state == "queued"
    q.cancel_queued()
    assert r2.state == "cancelled"
    for _ in range(120):
        r, _ = q.request("context", {}, origin="agent", turn_id=None)
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


def test_unknown_param_rejected_naming_allowed():
    e = rejected(validate_params, "compact", {"x": 1}, allow_force=False)
    assert e.code == "invalid_params" and "instructions" in e.message
    e = rejected(validate_params, "reload-plugins", {"bad": 1}, allow_force=True)
    assert e.code == "invalid_params" and "force" in e.message
    e = rejected(validate_params, "context", {"a": 1}, allow_force=False)
    assert e.code == "invalid_params" and "takes no params" in e.message


def test_force_and_instruction_limits():
    assert (
        rejected(validate_params, "reload-plugins", {"force": True}, allow_force=False).code
        == "force_not_allowed"
    )
    assert validate_params("reload-plugins", {"force": True}, allow_force=True) == {"force": True}
    for bad in ("a\nb", "a\rb", "a\x1bb", "x" * 2001):
        assert (
            rejected(validate_params, "compact", {"instructions": bad}, allow_force=False).code
            == "invalid_params"
        )
    validate_params("compact", {"instructions": "x" * 2000}, allow_force=False)


def test_duplicate_queued_returns_existing():
    q = CommandQueue(cfg())
    a, new = q.request("compact", {"instructions": "first"}, origin="agent", turn_id="t1")
    assert new
    b, new = q.request("compact", {"instructions": "second"}, origin="client", turn_id="t1")
    assert b is a and not new and a.args == {"instructions": "first"}
    assert len(q.pending()) == 1


def test_queue_cap_and_terminal_records():
    q = CommandQueue(cfg())
    first, _ = q.request("compact", {}, origin="agent", turn_id="t")
    for n in ("reload-plugins", "reload-skills", "context"):
        q.request(n, {}, origin="agent", turn_id="t")
    assert (
        rejected(q.request, "mcp", {"action": "reconnect"}, origin="agent", turn_id="t").code
        == "queue_full"
    )
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
