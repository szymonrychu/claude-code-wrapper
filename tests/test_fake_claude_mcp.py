"""Fake claude MCP tool step and the World internal-API forward."""
# ruff: noqa: F811

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest
from test_fake_claude import (  # type: ignore[import-not-found]
    env,  # noqa: F401
    hook_settings,
    spawn,  # noqa: F401
    transcript_lines,
    work,  # noqa: F401
)

TOOL_REPLY = {
    "result": {"content": [{"type": "text", "text": "queued "}, {"type": "text", "text": "ok"}]}
}


class Stub:
    """A JSON-RPC-over-HTTP MCP stub; records every request (method, headers, body)."""

    def __init__(self, tool_reply=None):
        self.seen: list[dict] = []
        self.tool_reply = tool_reply or TOOL_REPLY
        stub = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.seen.append({"headers": dict(self.headers), "body": body})
                method = body.get("method")
                if method == "server/discover":
                    out = {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {"code": -32601, "message": "no"},
                    }
                elif method == "initialize":
                    out = {"jsonrpc": "2.0", "id": body["id"], "result": {"capabilities": {}}}
                elif method == "tools/call":
                    out = {"jsonrpc": "2.0", "id": body["id"], **stub.tool_reply}
                else:
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/internal/mcp"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.close()


def configure(env, url=None, headers=None):  # noqa: F811
    servers = {}
    if url:
        servers["ccw"] = {"type": "http", "url": url, "headers": headers or {}}
    (Path(env["CLAUDE_CONFIG_DIR"]) / ".claude.json").write_text(
        json.dumps({"mcpServers": servers})
    )


def run(spawn, scenario_file, steps, **extra):  # noqa: F811
    sc = scenario_file(
        {"boot": {"bypass_dialog": False}, "turns": [{"match": ".*", "steps": steps}]}
    )
    p = spawn(FAKE_CLAUDE_SCENARIO=str(sc), **extra)
    p.wait_for("? for shortcuts")
    p.submit("go")
    return p


def step(**kw):
    return {"mcp": {"server": "ccw", "tool": "command", "arguments": {"name": "compact"}, **kw}}


def results_of(lines):
    return [
        c
        for x in lines
        if x["type"] == "user" and isinstance(x["message"]["content"], list)
        for c in x["message"]["content"]
        if c.get("type") == "tool_result"
    ]


def mcp_log(env):  # noqa: F811
    f = Path(env["CLAUDE_CONFIG_DIR"]) / "fake-mcp.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def record_hooks(env, out):  # noqa: F811
    hook_settings(
        env["CLAUDE_CONFIG_DIR"],
        {
            ev: [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": f"cat >> {out}; echo >> {out}"}],
                }
            ]
            for ev in ("PreToolUse", "PostToolUse", "PostToolUseFailure")
        },
    )
    return lambda: [json.loads(x) for x in out.read_text().splitlines()]


def test_mcp_step_expands_env_and_calls_tool(
    spawn,
    env,
    work,
    stub,
    tmp_path,
    scenario_file,  # noqa: F811
):
    configure(
        env,
        "${STUB_BASE}/internal/mcp",
        {"Authorization": "Bearer ${CCW_TOKEN}", "X-Gone": "a${UNSET_VAR_X}b"},
    )
    base = stub.url.removesuffix("/internal/mcp")
    pls = record_hooks(env, tmp_path / "h.out")
    run(spawn, scenario_file, [step()], STUB_BASE=base, CCW_TOKEN="tok-123")
    methods = [r["body"].get("method") for r in stub.seen]
    assert methods == ["server/discover", "initialize", "notifications/initialized", "tools/call"]
    init = stub.seen[1]["body"]["params"]
    assert init["protocolVersion"] == "2025-11-25"
    assert "id" not in stub.seen[2]["body"]
    assert stub.seen[3]["body"]["params"] == {"name": "command", "arguments": {"name": "compact"}}
    for r in stub.seen:
        h = {k.lower(): v for k, v in r["headers"].items()}
        assert h["authorization"] == "Bearer tok-123" and h["x-gone"] == "ab"
        assert h["content-type"] == "application/json"
        assert h["accept"] == "application/json, text/event-stream"
    [entry] = mcp_log(env)
    assert entry["url"] == stub.url and entry["headers"]["Authorization"] == "Bearer tok-123"
    assert [r["method"] for r in entry["requests"]] == methods
    assert len(entry["responses"]) == len(entry["requests"])
    lines = transcript_lines(env["CLAUDE_CONFIG_DIR"], work)
    use = [
        c
        for x in lines
        if x["type"] == "assistant"
        for c in x["message"]["content"]
        if c["type"] == "tool_use"
    ]
    assert use[0]["name"] == "mcp__ccw__command" and use[0]["input"] == {"name": "compact"}
    [res] = results_of(lines)
    assert res["content"] == "queued ok" and res["is_error"] is False
    got = pls()
    assert [x["hook_event_name"] for x in got] == ["PreToolUse", "PostToolUse"]
    assert got[0]["tool_name"] == "mcp__ccw__command" and got[0]["tool_input"] == {
        "name": "compact"
    }
    assert "agent_id" not in got[0]


def test_mcp_step_missing_server(
    spawn,
    env,
    work,
    stub,
    scenario_file,  # noqa: F811
):
    configure(env)
    run(spawn, scenario_file, [step()])
    [res] = results_of(transcript_lines(env["CLAUDE_CONFIG_DIR"], work))
    assert res["is_error"] is True and 'MCP server "ccw" is not configured' in res["content"]
    assert stub.seen == [] and mcp_log(env) == []


def test_mcp_step_denied_skips_http(
    spawn,
    env,
    work,
    stub,
    tmp_path,
    scenario_file,  # noqa: F811
):
    configure(env, stub.url)
    deny = tmp_path / "deny.sh"
    deny.write_text('cat >/dev/null; echo "no mcp today" >&2; exit 2\n')
    hook_settings(
        env["CLAUDE_CONFIG_DIR"],
        {"PreToolUse": [{"matcher": "mcp__ccw__.*", "hooks": [{"command": f"sh {deny}"}]}]},
    )
    run(spawn, scenario_file, [step()])
    [res] = results_of(transcript_lines(env["CLAUDE_CONFIG_DIR"], work))
    assert res["is_error"] is True and "no mcp today" in res["content"]
    assert stub.seen == []


@pytest.mark.parametrize(
    "reply",
    [
        {"result": {"isError": True, "content": [{"type": "text", "text": "bad name"}]}},
        {"error": {"code": -32000, "message": "rpc broke"}},
    ],
)
def test_mcp_step_result_and_error_reach_transcript(
    spawn,
    env,
    work,
    tmp_path,
    scenario_file,
    reply,  # noqa: F811
):
    stub = Stub(reply)
    try:
        configure(env, stub.url)
        pls = record_hooks(env, tmp_path / "h.out")
        run(spawn, scenario_file, [step()])
    finally:
        stub.close()
    [res] = results_of(transcript_lines(env["CLAUDE_CONFIG_DIR"], work))
    assert res["is_error"] is True and (
        "bad name" in res["content"] or "rpc broke" in res["content"]
    )
    assert [x["hook_event_name"] for x in pls()] == ["PreToolUse", "PostToolUseFailure"]


def test_mcp_step_agent_attribution(
    spawn,
    env,
    work,
    stub,
    tmp_path,
    scenario_file,  # noqa: F811
):
    configure(env, stub.url)
    pls = record_hooks(env, tmp_path / "h.out")
    sid = "44444444-4444-4444-8444-444444444444"
    sc = scenario_file(
        {
            "boot": {"bypass_dialog": False},
            "turns": [{"match": ".*", "steps": [step(agent={"id": "a9", "type": "Explore"})]}],
        }
    )
    p = spawn(["--session-id", sid], FAKE_CLAUDE_SCENARIO=str(sc))
    p.wait_for("? for shortcuts")
    p.submit("go")
    proj = re.sub(r"[^A-Za-z0-9]", "-", work)
    f = Path(env["CLAUDE_CONFIG_DIR"]) / "projects" / proj / sid / "subagents" / "agent-a9.jsonl"
    rows = [json.loads(x) for x in f.read_text().splitlines()]
    assert len(rows) == 2 and all(r["isSidechain"] is True for r in rows)
    assert results_of(rows)[0]["content"] == "queued ok"
    pre = pls()[0]
    assert pre["agent_id"] == "a9" and pre["agent_type"] == "Explore"


async def test_world_forwards_internal_routes(world):
    from claude_code_wrapper.manager import CreateSessionRequest

    m = world.manager(world.cfg())
    snap = await m.create(CreateSessionRequest())
    sid = snap["session_id"]
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{world.url}/internal/healthz", headers={"X-CCW-Session": "nope"})
        assert r.status_code == 403
        r = await c.get(f"{world.url}/internal/healthz", headers={"X-CCW-Session": sid})
        assert r.status_code == 200 and r.json() == {"status": "ok"}
        r = await c.get(f"{world.url}/internal/nothing?x=1", headers={"X-CCW-Session": sid})
        assert r.status_code == 404
        r = await c.post(f"{world.url}/internal/healthz", headers={"X-CCW-Session": sid})
        assert r.status_code == 405
