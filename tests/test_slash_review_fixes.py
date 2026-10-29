from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import World
from test_command_runner import finished, inputs, start, types

from claude_code_wrapper.commands import MAX_COMMAND_ARGS, CommandRejected, validate
from claude_code_wrapper.pty_proc import paste_bytes

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()
EXAMPLE = (ROOT / "src/claude_code_wrapper/data/config.example.yaml").read_text()


@pytest.mark.parametrize(
    "bad",
    ["x\r/exit", "x\x85y", "x\x1by", "x\x1b[201~", "x\x00y", "a\u2028b", "a\u2029b", "a" * 20001],
)
def test_args_reject_control_chars_and_length(bad):
    with pytest.raises(CommandRejected) as e:
        validate("compact", bad, allow_force=True)
    assert e.value.code == "invalid_command"


def test_args_keep_newlines_and_tabs():
    assert validate("compact", "x\n/exit", allow_force=False) == "x\n/exit"
    assert validate("mcp", "reconnect\tgh", allow_force=False) == "reconnect\tgh"
    assert validate("compact", "a" * MAX_COMMAND_ARGS, allow_force=False) == "a" * 20000


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


async def test_args_smuggling_never_typed(world: World):
    m, sid, _ = await start(world)
    router = m.router(sid)
    writes: list[bytes] = []
    pty = router.session.pty
    write_locked = pty._write_locked

    async def recording(data: bytes) -> None:
        writes.append(bytes(data))
        await write_locked(data)

    pty._write_locked = recording
    rec = router.request_command("compact", "keep\n/exit", origin="client")
    await finished(world, rec)
    assert rec.state == "completed" and rec.args == "keep\n/exit"
    await world.until(lambda: router.session.state == "ready")
    # one paste and one CR: the fake took one submitted line, and /exit never ran on its own
    assert writes == [paste_bytes("/compact keep\n/exit"), b"\r"]
    assert [i["text"] for i in inputs(world, sid)] == ["/compact keep\n/exit"]
    assert router.session.state == "ready"
    before = types(world, sid)
    for bad in ("x\x1b[201~\r/exit", "x\x1b"):
        with pytest.raises(CommandRejected) as e:
            router.request_command("compact", bad, origin="client")
        assert e.value.code == "invalid_command"
    assert writes == [paste_bytes("/compact keep\n/exit"), b"\r"]
    assert len(inputs(world, sid)) == 1 and types(world, sid) == before
    assert router.commands.list() == [rec]


async def test_tool_schema_is_flat():
    import test_mcp_server as t

    for agent in (["compact", "mcp", "reload-plugins"], ["*"]):
        async with t.client(t.FakeManager(agent)) as c:
            r = await c.post("/internal/mcp", json=t.rpc("tools/list"), headers=t.H)
        (tool,) = r.json()["result"]["tools"]
        s = tool["inputSchema"]
        assert not {"oneOf", "anyOf", "allOf"} & set(s)
        assert set(s["properties"]) == {"name", "args"}
        assert s["properties"]["args"]["type"] == "string"
        assert s["additionalProperties"] is False and s["required"] == ["name"]


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
    assert res["isError"] is True and "allowed: name, args" in res["content"][0]["text"]
    assert m.r.calls == []
