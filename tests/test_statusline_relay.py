from __future__ import annotations

import http.server
import io
import json
import sys
import threading
from typing import Any

import httpx

from claude_code_wrapper import statusline
from claude_code_wrapper.bootstrap.settings import render_settings
from claude_code_wrapper.config import Config
from claude_code_wrapper.internal_api import create_internal_app
from claude_code_wrapper.router import SessionRouter

TOKEN = "tok"
PAYLOAD = {
    "model": {"id": "claude-x", "display_name": "X"},
    "context_window": {
        "used_percentage": 12.5,
        "context_window_size": 200000,
        "current_usage": {"input_tokens": 10},
    },
}


class _Capture(http.server.BaseHTTPRequestHandler):
    seen: list[tuple[str, dict[str, str], bytes]] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a: Any) -> None:
        pass


def _run(monkeypatch, capsys, stdin: bytes) -> str:
    class _In:
        buffer = io.BytesIO(stdin)

    monkeypatch.setattr(sys, "stdin", _In())
    assert statusline.main() == 0
    return capsys.readouterr().out


def _env(monkeypatch, url: str, config_dir) -> None:
    monkeypatch.setenv("CCW_INTERNAL_URL", url)
    monkeypatch.setenv("CCW_SESSION_ID", "s1")
    monkeypatch.setenv("CCW_HOOK_TOKEN", TOKEN)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))


def test_cli_posts_stdin_to_internal_route(monkeypatch, capsys, tmp_path) -> None:
    _Capture.seen = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _env(monkeypatch, f"http://127.0.0.1:{server.server_port}", tmp_path)
        body = json.dumps(PAYLOAD).encode()
        out = _run(monkeypatch, capsys, body)
    finally:
        server.shutdown()
    assert out == ""
    ((path, headers, got),) = _Capture.seen
    assert path == "/internal/statusline"
    assert got == body
    assert headers["x-ccw-session"] == "s1"
    assert headers["x-ccw-hook-token"] == TOKEN


def test_chained_command_output_is_printed(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.delenv("CCW_INTERNAL_URL", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    cmd = f"{sys.executable} -c \"import sys; print('chained:' + sys.stdin.read())\""
    statusline.chain_path(tmp_path).write_text(json.dumps({"command": cmd}))
    out = _run(monkeypatch, capsys, b"hello")
    assert out == "chained:hello\n"


def test_post_failure_is_best_effort(monkeypatch, capsys, tmp_path) -> None:
    _env(monkeypatch, "http://127.0.0.1:9", tmp_path)
    monkeypatch.setattr(statusline, "POST_TIMEOUT", 0.2)
    assert _run(monkeypatch, capsys, b"{}") == ""
    statusline.chain_path(tmp_path).write_text(json.dumps({"command": "echo still"}))
    assert _run(monkeypatch, capsys, b"{}") == "still\n"


def test_settings_install_and_chain(tmp_path) -> None:
    doc = render_settings(tmp_path, Config(), relay_command="/r")
    assert doc["statusLine"] == {"type": "command", "command": "ccw statusline"}
    assert not statusline.chain_path(tmp_path).exists()

    cfg = Config.model_validate(
        {
            "claude": {
                "settings": {"statusLine": {"type": "command", "command": "mine", "padding": 2}}
            }
        }
    )
    d = tmp_path / "b"
    d.mkdir()
    doc = render_settings(d, cfg, relay_command="/r")
    assert doc["statusLine"] == {"type": "command", "command": "ccw statusline", "padding": 2}
    assert json.loads(statusline.chain_path(d).read_text()) == {"command": "mine"}


def test_chain_taken_after_overlay_and_idempotent(tmp_path) -> None:
    cfg = Config.model_validate(
        {"claude": {"settings": {"statusLine": {"type": "command", "command": "mine"}}}}
    )
    first = render_settings(tmp_path, cfg, relay_command="/r")
    again = render_settings(tmp_path, cfg, relay_command="/r")
    assert first["statusLine"] == again["statusLine"]
    assert json.loads(statusline.chain_path(tmp_path).read_text()) == {"command": "mine"}
    # No overlay on a re-render: the installed relay is not chained to itself.
    third = render_settings(tmp_path, Config(), relay_command="/r")
    assert third["statusLine"]["command"] == "ccw statusline"
    assert json.loads(statusline.chain_path(tmp_path).read_text()) == {"command": "mine"}


class _Router:
    def __init__(self) -> None:
        self.got: list[dict[str, Any]] = []

    def on_statusline(self, values: dict[str, Any]) -> None:
        self.got.append(values)


class _Manager:
    def __init__(self) -> None:
        self.r = _Router()

    def hook_token_matches(self, sid: str, token: str | None) -> bool:
        return sid == "s1" and token == TOKEN

    def router(self, sid: str) -> _Router:
        return self.r


async def test_internal_route_validates_and_dispatches() -> None:
    mgr = _Manager()
    app = create_internal_app(mgr)  # type: ignore[arg-type]
    h = {"X-CCW-Session": "s1", "X-CCW-Hook-Token": TOKEN}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)), base_url="http://t"
    ) as c:
        r = await c.post("/internal/statusline", json=PAYLOAD, headers=h)
        assert r.status_code == 204
        assert mgr.r.got == [
            {
                "used_percentage": 12.5,
                "context_window_size": 200000,
                "current_usage": {"input_tokens": 10},
                "model": {"id": "claude-x", "display_name": "X"},
            }
        ]
        nulls = await c.post("/internal/statusline", json={"context_window": None}, headers=h)
        assert nulls.status_code == 204
        assert mgr.r.got[-1]["used_percentage"] is None
        bad = await c.post(
            "/internal/statusline", json={"context_window": {"used_percentage": "x"}}, headers=h
        )
        assert bad.status_code == 400
        assert (await c.post("/internal/statusline", json=[1], headers=h)).status_code == 400
        forbidden = await c.post("/internal/statusline", json=PAYLOAD)
        assert forbidden.status_code == 403
    assert len(mgr.r.got) == 2


def test_router_on_statusline_stores_latest() -> None:
    router = SessionRouter.__new__(SessionRouter)
    router.context = None
    router.on_statusline({"used_percentage": 3.0})
    router.on_statusline({"used_percentage": 4.0})
    assert router.statusline == {"used_percentage": 4.0}
