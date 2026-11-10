"""`claude.secret_env`: env names whose values only the claude child holds, redacted by value.

A listed name is redacted whatever its name looks like and however short its value is; the
name-pattern redaction (`SECRET_KEY_RE`) still applies on top. Web-terminal shells and every
child that runs repo-controlled code (git, setup commands, mise, plugin installs) never get a
listed name.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from pathlib import Path

import httpx
import pytest
from conftest import World
from pydantic import ValidationError
from test_api_turns import TOKEN
from test_dialog_auto_dismiss import show
from test_permission_relay import of_type
from test_terminals import base_service_env, run

from claude_code_wrapper import service as service_mod
from claude_code_wrapper import session as session_mod
from claude_code_wrapper import tooling
from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.claude_env import ClaudeEnv, without_secrets
from claude_code_wrapper.config import CommandsOverride, Config
from claude_code_wrapper.manager import CreateSessionRequest, SessionManager
from claude_code_wrapper.redact import Redactor
from claude_code_wrapper.session import _redactor_for

#: Long value under a name the secret pattern does not match.
FOO = "CANARY-foo-value-4417"
#: Short value: below the name-pattern redaction's length floor.
BAR = "Zq7"
NAMES = ["FOO", "BAR"]


def test_secret_env_defaults_to_empty() -> None:
    assert Config.model_validate({}).claude.secret_env == []


@pytest.mark.parametrize("name", ["foo", "1FOO", "FOO-BAR", "", "FOO*"])
def test_secret_env_names_are_env_names(name: str) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"claude": {"secret_env": [name]}})


def test_redactor_listed_values_have_no_length_floor() -> None:
    r = Redactor()
    r.add(["ab"])
    assert r.redact("ab cd") == "ab cd"
    r.add(["ab", ""], min_len=1)
    assert r.redact("ab cd") == "*** cd"


def test_session_redactor_masks_listed_and_pattern_names() -> None:
    env = {"FOO": FOO, "BAR": BAR, "GIT_TOKEN": "pattern-token-value", "PLAIN": "plain-value-1"}
    r = _redactor_for(env, NAMES)
    out = r.redact(f"{FOO} {BAR} pattern-token-value plain-value-1")
    assert out == "*** *** *** plain-value-1"
    assert _redactor_for(env).redact(f"{FOO} {BAR}") == f"{FOO} {BAR}"


def test_without_secrets_drops_listed_names() -> None:
    env = {"FOO": FOO, "BAR": BAR, "PLAIN": "p", "CCW_AUTH_TOKEN": "x"}
    assert without_secrets(env, NAMES) == {"PLAIN": "p"}
    assert without_secrets(env) == {"FOO": FOO, "BAR": BAR, "PLAIN": "p"}


def test_listed_names_masked_in_config_and_env_views() -> None:
    cfg = Config.model_validate(
        {
            "claude": {
                "secret_env": NAMES,
                "env": {"extra": {"FOO": FOO, "PLAIN_V": "plain"}},
                "mcp_servers": {
                    "s": {"type": "stdio", "command": "x", "env": {"BAR": BAR, "P": "p"}}
                },
            }
        }
    )
    view = json.dumps(cfg.redacted())
    assert FOO not in view and BAR not in view and '"PLAIN_V": "plain"' in view
    env = ClaudeEnv(extra={"FOO": FOO, "PLAIN_V": "plain"})
    assert env.redacted(NAMES)["extra"] == {"FOO": "***", "PLAIN_V": "plain"}


def _scenario() -> dict:
    return {
        "turns": [
            {
                "match": "^show",
                "steps": [
                    {"text": f"foo={FOO} bar={BAR}"},
                    {"thinking": f"thinking {FOO} {BAR}"},
                    {
                        "tool": "Bash",
                        "input": {"command": f"echo {FOO} {BAR}"},
                        "result": f"FOO={FOO}\nBAR={BAR}",
                    },
                ],
            }
        ]
    }


async def _session(world: World, tmp_path: Path, **sections) -> tuple[SessionManager, str]:
    service_env = {**base_service_env(), "FOO": FOO, "BAR": BAR, "PLAIN_PASS": "plain-pass"}
    claude = {"passthrough_env": ["FOO", "BAR", "PLAIN_PASS"], "secret_env": NAMES}
    claude.update(sections.pop("claude", {}))
    cfg = world.cfg(_scenario(), claude=claude, **sections)
    m = SessionManager(cfg, internal_url=world.url, service_env=service_env, redactor=Redactor())
    world.managers.append(m)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    return m, sid


async def test_values_reach_claude_only_and_are_redacted(world: World, tmp_path: Path) -> None:
    setup_dump = tmp_path / "setup.env"
    hook = {
        "type": "command",
        "run": 'printf \'{"output": "hook %s %s"}\' "$FOO" "$BAR"',
        "blocking": True,
    }
    m, sid = await _session(
        world,
        tmp_path,
        init={"setup_commands": [f'env > "{setup_dump}"']},
        hooks={"onToolResult": [hook], "onTurnComplete": [hook]},
    )
    ctx = m._active[sid]
    # the claude child (and only it) holds the values
    child = m._child_env(ctx)
    assert child["FOO"] == FOO and child["BAR"] == BAR
    pid = ctx.session.pty.pid
    environ = Path(f"/proc/{pid}/environ")
    if environ.exists():
        entries = environ.read_bytes().split(b"\0")
        assert f"FOO={FOO}".encode() in entries and f"BAR={BAR}".encode() in entries

    record = await m.router(sid).submit_turn("show me")
    await world.wait_turn(m, sid)
    await world.until(
        lambda: sum(e["type"] == "hook_result" for e in world.events(sid)) >= 2,
        what="both hook results",
    )

    events = (world.session_dir(sid) / "events.jsonl").read_text()
    for value in (FOO, BAR):
        assert value not in events
    types = {e["type"] for e in world.events(sid)}
    assert {"assistant_text", "tool_result", "hook_result"} <= types
    texts = [e["data"] for e in world.events(sid) if e["type"] in ("assistant_text", "tool_result")]
    assert any("foo=*** bar=***" in json.dumps(d) for d in texts)
    hooks = [e["data"] for e in world.events(sid) if e["type"] == "hook_result"]
    assert any(d.get("output") == "hook *** ***" for d in hooks)
    turns = (world.session_dir(sid) / "turns.jsonl").read_text()
    assert FOO not in turns and BAR not in turns
    assert record.turn_id in turns

    # screen tails (session and router views)
    assert FOO in ctx.session.ring.text()  # the screen really showed it
    tail = ctx.session.tail(4000)
    assert "foo=*** bar=***" in tail and FOO not in tail and BAR not in tail
    assert FOO not in m.redactor.redact(ctx.session.ring.text())

    # repo-controlled children never get a listed name
    for gitconfig in (None, ctx.session_dir / "gitconfig"):
        env = m._tool_env(ctx, gitconfig)
        assert "FOO" not in env and "BAR" not in env and env["PLAIN_PASS"] == "plain-pass"
    seen = setup_dump.read_text()
    assert not re.search(r"(^|\n)(FOO|BAR)=", seen) and FOO not in seen
    assert "PLAIN_PASS=plain-pass" in seen

    # the web-terminal shell gets neither name nor value
    info = await m.terminals.create(sid)
    att = m.terminals.attach(sid, info.terminal_id)
    out = (att.snapshot + await run(att, "env; echo ENVEND$((2+3))", b"ENVEND5")).decode(
        "utf-8", "replace"
    )
    assert not re.search(r"(^|[\r\n])(FOO|BAR)=", out) and FOO not in out
    assert re.search(r"(^|[\r\n])PLAIN_PASS=plain-pass\r?\n", out)


async def test_listed_extra_and_override_values_redacted(world: World, tmp_path: Path) -> None:
    m, sid = await _session(
        world, tmp_path, claude={"env": {"extra": {"CFG_SECRET_X": "cfgvalue-1"}}}
    )
    # name-pattern redaction still applies to passthrough names matching SECRET_KEY_RE
    assert m.redactor.redact("cfgvalue-1") == "***"
    cfg = world.cfg(
        _scenario(),
        claude={"secret_env": ["OVR"], "env": {"extra": {"PLAIN_V": "plain"}}},
    )
    m2 = SessionManager(cfg, internal_url=world.url, service_env=base_service_env(),
                        redactor=Redactor())  # fmt: skip
    world.managers.append(m2)
    sid2 = (await m2.create(CreateSessionRequest(env={"extra": {"OVR": "ov1"}})))["session_id"]
    assert m2.redactor.redact("x ov1 y") == "x *** y"
    assert m2._child_env(m2._active[sid2])["OVR"] == "ov1"
    assert "OVR" not in m2._tool_env(m2._active[sid2], None)
    meta = (world.session_dir(sid2) / "session.json").read_text()
    assert "ov1" not in meta
    snap = json.dumps(m2.get(sid2))
    assert "ov1" not in snap


async def test_global_mise_install_drops_listed_names(tmp_path, monkeypatch) -> None:
    seen: dict[str, str] = {}

    async def fake_install(mise_cfg, env):
        seen.update(env)
        raise RuntimeError("stop here")

    monkeypatch.setattr(tooling, "install_global", fake_install)
    monkeypatch.setattr(tooling, "render_global_mise", lambda cfg: None)
    monkeypatch.setenv("FOO", FOO)
    monkeypatch.setenv("PLAIN_PASS", "plain-pass")
    cfg = Config.model_validate(
        {
            "paths": {
                "workdir_root": str(tmp_path / "w"),
                "sessions_root": str(tmp_path / "s"),
            },
            "claude": {"passthrough_env": ["FOO", "PLAIN_PASS"], "secret_env": ["FOO"]},
        }
    )
    with pytest.raises(RuntimeError, match="stop here"):
        await service_mod.run_service(cfg)
    assert seen.get("PLAIN_PASS") == "plain-pass" and "FOO" not in seen
    assert os.environ["FOO"] == FOO


#: The canary of the permission relay and dialog surface (T-98): a listed secret_env value.
RELAY = "CANARY-t98-relay-0001"


def _relay_scenario() -> dict:
    """A gated Bash call whose input and permission suggestions hold the listed value."""
    rule = {"toolName": "Bash", "ruleContent": f"echo {RELAY}"}
    suggestion = {"type": "addRules", "rules": [rule], "behavior": "allow"}
    step = {
        "tool": "Bash",
        "input": {"command": f"echo {RELAY}"},
        "result": "ok",
        "permission": {"suggestions": [suggestion]},
    }
    return {"turns": [{"match": "^go", "steps": [step, {"text": "after the tool"}]}]}


async def test_permission_relay_and_dialog_never_show_a_listed_value(
    world: World, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Canary: GET /v1/sessions/{id}/dialog (permission.tool_input, suggestions, the dialog
    tail), permission_requested / permission_resolved (with terminal_observed) /
    dialog_auto_dismissed, and the relay and API logs never carry a secret_env value, plain or
    base64."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(session_mod, "ORPHAN_SETTLE_SECONDS", 0.3)
    service_env = {**base_service_env(), "RELAYV": RELAY}
    cfg = world.cfg(
        _relay_scenario(),
        server={"auth_token": TOKEN},
        claude={"passthrough_env": ["RELAYV"], "secret_env": ["RELAYV"]},
    )
    m = SessionManager(cfg, internal_url=world.url, service_env=service_env, redactor=Redactor())
    world.managers.append(m)
    over = CommandsOverride(dialog_auto_dismiss_seconds=0.4)
    sid = (await m.create(CreateSessionRequest(commands=over)))["session_id"]
    router = m.router(sid)
    session = router.session
    bodies: list[str] = []
    headers = {"Authorization": f"Bearer {TOKEN}"}
    transport = httpx.ASGITransport(app=create_app(m, m.cfg))
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        await world.until(lambda: session.state == "ready", 15)
        r = await c.post(f"/v1/sessions/{sid}/turns", json={"text": "go"})
        assert r.status_code == 202, r.text
        await world.until(lambda: of_type(world, sid, "permission_requested"), 15)
        r = await c.get(f"/v1/sessions/{sid}/dialog")
        bodies.append(r.text)
        view = r.json()
        assert view["kind"] == "permission"
        assert "***" in view["permission"]["tool_input"]
        assert "***" in json.dumps(view["permission"]["suggestions"])
        # The person at the terminal picks "1. Yes": a terminal resolution (terminal_observed).
        await session.pty.write(b"1")
        await world.wait_turn(m, sid)
        await world.until(lambda: of_type(world, sid, "permission_resolved"), 15)
        (got,) = of_type(world, sid, "permission_resolved")
        assert (got["by"], got["terminal_observed"]) == ("terminal", "allow")
        await world.until(lambda: not session.permission_prompt_open(), 10, "lock lifted")
        # An idle footer dialog whose screen tail holds the value (the permission prompt
        # scrolled out of the tail first, so it is a plain dialog the idle timer dismisses).
        show(session, "tool output\r\n" * (session_mod.DIALOG_TAIL_CHARS // 10))
        show(session, f"Select model\r\n {RELAY}\r\n 1. Default\r\n Esc to cancel\r\n")
        assert RELAY in session.ring.text()  # the screen really shows it
        r = await c.get(f"/v1/sessions/{sid}/dialog")
        bodies.append(r.text)
        view = r.json()
        assert view["kind"] == "dialog" and "***" in view["tail"]
        await world.until(lambda: of_type(world, sid, "dialog_auto_dismissed"), 10)
        r = await c.get(f"/v1/sessions/{sid}")
        bodies.append(r.text)

    types = {e["type"] for e in world.events(sid)}
    assert {"permission_requested", "permission_resolved", "dialog_auto_dismissed"} <= types
    requested = of_type(world, sid, "permission_requested")[0]
    assert "***" in requested["tool_input"] and "***" in json.dumps(requested["suggestions"])
    haystacks = {
        "events": (world.session_dir(sid) / "events.jsonl").read_text(),
        "turns": (world.session_dir(sid) / "turns.jsonl").read_text(),
        "responses": "\n".join(bodies),
        "logs": caplog.text,
    }
    b64 = base64.b64encode(RELAY.encode()).decode().rstrip("=")
    for where, text in haystacks.items():
        assert RELAY not in text, where
        assert b64 not in text, where
