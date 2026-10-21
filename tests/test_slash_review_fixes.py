from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from claude_code_wrapper.commands import (
    BUILTIN_COMMANDS,
    CommandRejected,
    params_schema,
    validate_params,
)

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()
EXAMPLE = (ROOT / "src/claude_code_wrapper/data/config.example.yaml").read_text()


@pytest.mark.parametrize(
    "server", ["gh\n/compact", "gh\r/exit", "a b", "a;b", "--force", "x" * 65, "", "a b"]
)
def test_mcp_server_param_rejected(server):
    with pytest.raises(CommandRejected) as e:
        validate_params("mcp", {"action": "reconnect", "server": server}, allow_force=False)
    assert e.value.code == "invalid_params"


def test_mcp_server_param_accepted():
    for ok in ("gh", "my.server_1:x-y", "a" * 64):
        validate_params("mcp", {"action": "reconnect", "server": ok}, allow_force=False)


def test_every_string_param_rejects_control_chars():
    for name, spec in BUILTIN_COMMANDS.items():
        for key, (kind, _d, _r) in spec.items():
            if kind != "string":
                continue
            base = {"action": "reconnect"} if name == "mcp" else {}
            with pytest.raises(CommandRejected):
                validate_params(name, {**base, key: "a\n/exit"}, allow_force=False)
            with pytest.raises(CommandRejected):
                validate_params(name, {**base, key: "a" * 5000}, allow_force=False)


def test_action_with_newline_rejected():
    with pytest.raises(CommandRejected):
        validate_params("mcp", {"action": "reconnect\n/exit"}, allow_force=False)


def test_params_schema_is_flat():
    s = params_schema(["compact", "mcp", "reload-plugins"])
    json.dumps(s)
    assert not {"oneOf", "anyOf", "allOf"} & set(s)
    assert s["properties"]["name"]["enum"] == ["compact", "mcp", "reload-plugins"]
    assert s["additionalProperties"] is False and s["required"] == ["name"]
    p = s["properties"]["params"]
    assert p["type"] == "object" and p["additionalProperties"] is False
    assert set(p["properties"]) == {"instructions", "force", "action", "server"}
    assert "compact" in p["properties"]["instructions"]["description"]
    assert "mcp" in p["properties"]["server"]["description"]
    assert set(params_schema(["compact"])["properties"]["params"]["properties"]) == {"instructions"}


def test_readme_command_events_use_id():
    for ev in ("queued", "started", "completed", "failed"):
        m = re.search(rf"### `command_{ev}`\n(.*?)(?=^### )", README, re.S | re.M)
        assert m and "`command_id`" not in m.group(1), ev
        assert "`id`" in m.group(1) and "`result`" in m.group(1) and "`output`" in m.group(1)


def test_readme_documents_subagent_bypass():
    body = README.split("\n## Slash commands\n")[1].split("\n## ", 1)[0]
    assert "PreToolUse" in body and "/internal/mcp" in body and "subagent" in body
    trust = README.split("\n## Trust model\n")[1].split("\n## ", 1)[0]
    assert "/internal/mcp" in trust


def test_example_config_commands_client_comment():
    line = next(x for x in EXAMPLE.splitlines() if "client: []" in x)
    assert "/sessions/" not in line and "public commands endpoint" in line


@pytest.mark.parametrize("bad", ["x\n/exit", "x\r/exit", "x y", "x\x85y", "x\x1by", "a" * 5000])
def test_string_params_reject_control_chars_and_bad_server(bad):
    with pytest.raises(CommandRejected):
        validate_params("compact", {"instructions": bad}, allow_force=True)
    with pytest.raises(CommandRejected):
        validate_params("mcp", {"action": "reconnect", "server": bad}, allow_force=True)
    for server in ("-x", "a b", "a/b", ""):
        with pytest.raises(CommandRejected):
            validate_params("mcp", {"action": "reconnect", "server": server}, allow_force=True)


def test_param_smuggling_never_typed():
    from claude_code_wrapper.commands import CommandQueue
    from claude_code_wrapper.config import CommandsConfig

    q = CommandQueue(CommandsConfig(agent=list(BUILTIN_COMMANDS), client=list(BUILTIN_COMMANDS)))
    attempts = [
        ("compact", {"instructions": "ok\n/exit"}),
        ("mcp", {"action": "reconnect", "server": "gh\n/compact"}),
        ("mcp", {"action": "reconnect", "server": "gh --force"}),
        ("reload-plugins", {"force": True}),
    ]
    for name, params in attempts:
        with pytest.raises(CommandRejected):
            q.request(name, params, origin="agent", turn_id="t1")
    assert q.pending() == [] and q.list() == []


def test_tool_schema_is_flat():
    s = params_schema(list(BUILTIN_COMMANDS))
    assert not {"oneOf", "anyOf", "allOf"} & set(s)
    assert set(s["properties"]) == {"name", "params"}
    assert s["properties"]["params"]["additionalProperties"] is False


async def test_unknown_top_level_argument_rejected():
    import test_mcp_server as t

    m = t.FakeManager(["compact"])
    async with t.client(m) as c:
        r = await c.post(
            "/internal/mcp",
            json=t.rpc("tools/call", {"name": "command", "arguments": {"name": "compact", "x": 1}}),
            headers=t.H,
        )
    res = r.json()["result"]
    assert res["isError"] is True and "params" in res["content"][0]["text"]
    assert m.r.calls == []
