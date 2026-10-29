"""ccw MCP server injection into the claude config, and the agent path end to end."""

import json
from pathlib import Path

import pytest
from conftest import World, jsonl, turns_of
from test_command_runner import commands_cfg, inputs, of_type
from test_command_turn_resume import ORDER, RESUMED, start, typed, types

from claude_code_wrapper.bootstrap import seed_claude_json
from claude_code_wrapper.bootstrap.settings import render_settings
from claude_code_wrapper.config import ClaudeConfig, Config, ConfigError, load_config
from claude_code_wrapper.manager import CreateSessionRequest, InvalidRequest

ENTRY = {
    "type": "http",
    "url": "${CCW_INTERNAL_URL}/internal/mcp",
    "headers": {
        "X-CCW-Session": "${CCW_SESSION_ID}",
        "X-CCW-Hook-Token": "${CCW_HOOK_TOKEN}",
    },
    "alwaysLoad": True,
}
ALLOW = "mcp__ccw__command"
SELF = {"name": "compact", "args": "keep the plan"}


def seeded(tmp_path: Path, **kw) -> dict:
    seed_claude_json(tmp_path / "c", tmp_path / "w", ClaudeConfig(), object(), **kw)
    return json.loads((tmp_path / "c" / ".claude.json").read_text())


def test_claude_json_has_ccw_server_with_env_refs(tmp_path):
    assert seeded(tmp_path, ccw_mcp=True)["mcpServers"]["ccw"] == ENTRY


def test_disabled_removes_ccw_server_and_allow(tmp_path):
    seeded(tmp_path, ccw_mcp=True)
    data = seeded(tmp_path, ccw_mcp=False)
    assert "ccw" not in data.get("mcpServers", {})
    doc = render_settings(tmp_path, Config(), relay_command="/r", ccw_mcp=True)
    assert ALLOW in doc["permissions"]["allow"]
    doc = render_settings(tmp_path, Config(), relay_command="/r", ccw_mcp=False)
    assert ALLOW not in doc["permissions"]["allow"]
    doc = render_settings(tmp_path, Config(), relay_command="/r", ccw_mcp=True)
    assert ALLOW in doc["permissions"]["allow"]
    doc = render_settings(tmp_path, Config(), relay_command="/r", ccw_mcp=False)
    assert ALLOW not in doc["permissions"]["allow"]


def test_seed_keeps_user_defined_ccw_entry(tmp_path):
    from claude_code_wrapper.config import McpHttp

    srv = McpHttp(type="http", url="http://user")
    cfg = ClaudeConfig(mcp_servers={"ccw": srv})
    seed_claude_json(tmp_path / "c", tmp_path / "w", cfg, object(), ccw_mcp=False)
    data = json.loads((tmp_path / "c" / ".claude.json").read_text())
    assert data["mcpServers"]["ccw"]["url"] == "http://user"


def test_seed_default_has_no_ccw(tmp_path):
    assert "ccw" not in seeded(tmp_path).get("mcpServers", {})


def test_settings_allow_ccw_command(tmp_path):
    doc = render_settings(tmp_path, Config(), relay_command="/r", ccw_mcp=True)
    assert doc["permissions"]["allow"].count(ALLOW) == 1
    doc = render_settings(tmp_path, Config(), relay_command="/r", ccw_mcp=True)
    assert doc["permissions"]["allow"].count(ALLOW) == 1
    doc = render_settings(tmp_path, Config(), relay_command="/r", ccw_mcp=False)
    assert ALLOW not in doc["permissions"]["allow"]


def test_reserved_ccw_server_name_rejected():
    srv = {"ccw": {"type": "http", "url": "http://x"}}
    Config.model_validate({"claude": {"mcp_servers": srv}})
    with pytest.raises(Exception) as exc:
        Config.model_validate(
            {"claude": {"mcp_servers": srv}, "sessions": {"commands": {"agent": ["compact"]}}}
        )
    assert "claude.mcp_servers.ccw" in str(exc.value) or "the name ccw is reserved" in str(
        exc.value
    )


def test_load_config_reports_path(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "claude: {mcp_servers: {ccw: {type: http, url: 'http://x'}}}\n"
        "sessions: {commands: {agent: [compact]}}\n"
    )
    with pytest.raises(ConfigError) as exc:
        load_config(p)
    assert "claude.mcp_servers.ccw: the name ccw is reserved for the ccw command server" in str(
        exc.value
    )


async def test_create_override_with_reserved_name_is_rejected(world: World):
    cfg = world.cfg({}, claude={"mcp_servers": {"ccw": {"type": "http", "url": "http://x"}}})
    m = world.manager(cfg)
    with pytest.raises(InvalidRequest, match="ccw"):
        await m.create(CreateSessionRequest(commands={"agent": ["compact"]}))


def mcp_step(**extra) -> dict:
    return {"mcp": {"server": "ccw", "tool": "command", "arguments": SELF, **extra}}


def tool_results(world: World, m, sid: str) -> list[dict]:
    claude_sid = world.meta(sid)["claude_session_id"]
    out = []
    for path in (world.session_dir(sid) / "config" / "projects").rglob("*.jsonl"):
        for line in jsonl(path):
            content = (line.get("message") or {}).get("content")
            if isinstance(content, list):
                out += [c for c in content if c.get("type") == "tool_result"]
    assert claude_sid
    return out


async def test_agent_self_compact_same_turn_e2e(world: World):
    go = {"match": "^go", "steps": [mcp_step(), {"text": "queued, ending"}]}
    m, sid, router = await start(world, [go, RESUMED])
    turn = await router.submit_turn("go")
    done = await world.wait_turn(m, sid)
    assert done.turn_id == turn.turn_id and done.final_text == "part two"
    assert [t.turn_id for t in turns_of(m, sid)] == [turn.turn_id]
    assert types(world, sid, "command_queued", *ORDER) == [
        "turn_started",
        "command_queued",
        "turn_deferred",
        "command_started",
        "compacted",
        "command_completed",
        "turn_resumed",
        "turn_completed",
    ]
    (deferred,) = of_type(world, sid, "turn_deferred")
    assert deferred["reason"] == "command"
    (resumed,) = of_type(world, sid, "turn_resumed")
    assert resumed["cause"] == "command"
    assert "/compact keep the plan" in typed(world, sid)
    fake = jsonl(world.session_dir(sid) / "config" / "fake-mcp.jsonl")
    assert fake[0]["headers"]["X-CCW-Session"] == sid
    token = fake[0]["headers"]["X-CCW-Hook-Token"]
    assert token and "${" not in token
    for p in (world.session_dir(sid) / "config").rglob("*"):
        if p.is_file() and p.name != "fake-mcp.jsonl":
            assert token not in p.read_bytes().decode(errors="replace"), p


async def test_token_never_written_to_disk(world: World):
    go = {"match": "^go", "steps": [mcp_step(), {"text": "queued, ending"}]}
    m, sid, router = await start(world, [go, RESUMED])
    await router.submit_turn("go")
    await world.wait_turn(m, sid)
    fake = jsonl(world.session_dir(sid) / "config" / "fake-mcp.jsonl")
    token = fake[0]["headers"]["X-CCW-Hook-Token"]
    assert token and "${" not in token
    for p in world.session_dir(sid).rglob("*"):
        if p.is_file() and p.name != "fake-mcp.jsonl":
            assert token not in p.read_bytes().decode(errors="replace"), p


async def test_agent_reload_plugins_e2e(world: World):
    step = mcp_step()
    step["mcp"]["arguments"] = {"name": "reload-plugins"}
    go = {"match": "^go", "steps": [step, {"text": "queued, ending"}]}
    m, sid, router = await start(world, [go, RESUMED])
    turn = await router.submit_turn("go")
    done = await world.wait_turn(m, sid)
    assert done.turn_id == turn.turn_id
    assert types(world, sid, "command_queued", *ORDER)[1:3] == ["command_queued", "turn_deferred"]
    (completed,) = (e["record"] for e in of_type(world, sid, "command_completed"))
    assert completed["result"] == "ok"
    assert completed["output"].startswith("Reloaded: ")
    assert [t.turn_id for t in turns_of(m, sid)] == [turn.turn_id]
    assert "reload-plugins" in [i["text"].removeprefix("/") for i in inputs(world, sid)][1]


async def test_subagent_call_refused_e2e(world: World):
    go = {
        "match": "^go",
        "steps": [mcp_step(agent={"id": "a1", "type": "Explore"}), {"text": "done"}],
    }
    m, sid, router = await start(world, [go])
    turn = await router.submit_turn("go")
    done = await world.wait_turn(m, sid)
    assert done.turn_id == turn.turn_id and done.final_text == "done"
    assert not of_type(world, sid, "command_queued")
    assert not of_type(world, sid, "turn_deferred")
    texts = [str(r.get("content")) for r in tool_results(world, m, sid)]
    assert any("main agent only" in t for t in texts), texts


async def test_disabled_session_has_no_ccw_server(world: World):
    go = {"match": "^go", "steps": [mcp_step(), {"text": "done"}]}
    scenario = {"turns": [go], "commands": {}}
    cfg = world.cfg(scenario, sessions={"commands": commands_cfg(agent=[])})
    m = world.manager(cfg)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    turn = await m.router(sid).submit_turn("go")
    done = await world.wait_turn(m, sid)
    assert done.turn_id == turn.turn_id
    cj = json.loads((world.session_dir(sid) / "config" / ".claude.json").read_text())
    assert "ccw" not in cj.get("mcpServers", {})
    texts = [str(r.get("content")) for r in tool_results(world, m, sid)]
    assert any("not configured" in t for t in texts), texts
    assert not of_type(world, sid, "command_queued")
