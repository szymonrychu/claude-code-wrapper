"""Tests for the ccw-hook relay."""

from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from claude_code_wrapper import hook_relay as hr


class _Srv:
    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n)
                outer.requests.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body,
                    }
                )
                item = outer.script.pop(0) if outer.script else (500, b"")
                if item == "hang":
                    import time

                    time.sleep(1.5)
                    item = (500, b"")
                code, payload = item
                self.send_response(code)
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCW_SESSION_ID", "s1")
    monkeypatch.setenv("CCW_HOOK_TOKEN", "tok")
    monkeypatch.setenv("CCW_RELAY_BACKOFF", "0.01")
    monkeypatch.delenv("CCW_TOOL_GATE_FAIL", raising=False)
    monkeypatch.delenv("CCW_GATE_TIMEOUT", raising=False)


def _run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    stdin: bytes = b"{}",
) -> tuple[int, str, str]:
    class In:
        buffer = io.BytesIO(stdin)

    monkeypatch.setattr("sys.stdin", In())
    rc = hr.main(argv)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def _ok(out: str = "", err: str = "", code: int = 0) -> tuple[int, bytes]:
    return 200, json.dumps({"stdout": out, "stderr": err, "exit_code": code}).encode()


def test_constants() -> None:
    assert hr.CLAUDE_HOOK_EVENTS == (
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "Stop",
        "SubagentStart",
        "SubagentStop",
    )
    assert hr.RELAY_PATH == "/internal/hooks/{event}"
    assert hr.HEADER_DELIVERY == "X-CCW-Delivery"
    assert json.loads(hr.deny_output("x")) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "x",
        }
    }


def test_success_passthrough(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    s = _Srv([_ok("OUT", "ERR", 2)])
    monkeypatch.setenv("CCW_INTERNAL_URL", s.url)
    rc, out, err = _run(monkeypatch, capsys, ["ccw-hook", "PreToolUse"], b'{"a": 1}')
    s.close()
    assert (rc, out, err) == (2, "OUT", "ERR")
    r = s.requests[0]
    assert r["path"] == "/internal/hooks/PreToolUse"
    assert r["body"] == b'{"a": 1}'
    assert r["headers"]["x-ccw-session"] == "s1"
    assert r["headers"]["x-ccw-hook-token"] == "tok"
    assert r["headers"]["content-type"] == "application/json"


def test_unknown_event_and_missing_env(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CCW_INTERNAL_URL", "http://127.0.0.1:1")
    assert _run(monkeypatch, capsys, ["h", "Nope"]) == (0, "", "")
    assert _run(monkeypatch, capsys, ["h"]) == (0, "", "")
    monkeypatch.delenv("CCW_INTERNAL_URL")
    assert _run(monkeypatch, capsys, ["h", "Stop"]) == (0, "", "")
    monkeypatch.setenv("CCW_INTERNAL_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("CCW_SESSION_ID")
    assert _run(monkeypatch, capsys, ["h", "Stop"]) == (0, "", "")


def test_retries_non_200_same_delivery_id(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    s = _Srv([(503, b""), _ok("fine")])
    monkeypatch.setenv("CCW_INTERNAL_URL", s.url)
    rc, out, _ = _run(monkeypatch, capsys, ["h", "Stop"])
    s.close()
    assert (rc, out) == (0, "fine")
    ids = {r["headers"]["x-ccw-delivery"] for r in s.requests}
    assert len(s.requests) == 2 and len(ids) == 1


def test_attempt_counts(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    s = _Srv([])
    monkeypatch.setenv("CCW_INTERNAL_URL", s.url)
    assert _run(monkeypatch, capsys, ["h", "UserPromptSubmit"]) == (0, "", "")
    assert len(s.requests) == 3
    s.requests.clear()
    assert _run(monkeypatch, capsys, ["h", "PostToolUse"]) == (0, "", "")
    assert len(s.requests) == 2
    s.close()


def test_gate_fail_deny(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CCW_INTERNAL_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("CCW_TOOL_GATE_FAIL", "deny")
    rc, out, _ = _run(monkeypatch, capsys, ["h", "PreToolUse"])
    assert rc == 0
    assert (
        json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
        == "claude-code-wrapper is unreachable"
    )
    rc, out, _ = _run(monkeypatch, capsys, ["h", "PostToolUse"])
    assert (rc, out) == (0, "")


def test_timeout_uses_gate_timeout(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    s = _Srv(["hang", "hang"])
    monkeypatch.setenv("CCW_INTERNAL_URL", s.url)
    monkeypatch.setenv("CCW_GATE_TIMEOUT", "0.2")
    rc, out, _ = _run(monkeypatch, capsys, ["h", "Stop"])
    s.close()
    assert (rc, out) == (0, "")
    assert len(s.requests) == 2


def test_garbage_200_body_is_failure(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    s = _Srv([(200, b"not json"), (200, b"[]")])
    monkeypatch.setenv("CCW_INTERNAL_URL", s.url)
    assert _run(monkeypatch, capsys, ["h", "Stop"]) == (0, "", "")
    s.close()
    assert len(s.requests) == 2
