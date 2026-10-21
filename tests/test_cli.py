from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claude_code_wrapper import __version__, cli
from claude_code_wrapper.tooling import ToolingError


@pytest.fixture(autouse=True)
def _fake_claude_on_path(fake_claude_bin: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """serve preflights `claude` on PATH; never depend on a real one being installed."""
    monkeypatch.setenv("PATH", f"{fake_claude_bin.parent}{os.pathsep}{os.environ['PATH']}")


def write_cfg(tmp_path: Path, body: str | None = None) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(
        body
        if body is not None
        else f"""
server: {{host: 127.0.0.1, port: 0, internal_port: 0, auth_token: secret-token-123}}
paths: {{workdir_root: {tmp_path}/wd, sessions_root: {tmp_path}/ss}}
limits: {{shutdown_timeout_seconds: 2}}
"""
    )
    return p


def test_version(capsys) -> None:
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_check_config_ok_prints_redacted(tmp_path: Path, capsys) -> None:
    assert cli.main(["check-config", "--config", str(write_cfg(tmp_path))]) == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["server"]["auth_token"] == "***"
    assert "secret-token-123" not in out


def test_check_config_invalid_exit_78(tmp_path: Path, capsys) -> None:
    bad = write_cfg(tmp_path, "server: {port: nope}\n")
    assert cli.main(["check-config", "--config", str(bad)]) == 78
    assert "server.port" in capsys.readouterr().err


def test_serve_invalid_config_exit_78(tmp_path: Path, capsys) -> None:
    bad = write_cfg(tmp_path, "bogus: 1\n")
    assert cli.main(["serve", "--config", str(bad)]) == 78
    assert "bogus" in capsys.readouterr().err


def test_serve_missing_config_exits_78(tmp_path: Path) -> None:
    assert cli.main(["serve", "--config", str(tmp_path / "none.yaml")]) == 78


def test_config_path_from_ccw_config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []

    async def fake(cfg, **kw):
        seen.append(cfg)

    monkeypatch.setattr(cli, "run_service", fake)
    env_cfg = write_cfg(tmp_path)
    monkeypatch.setenv("CCW_CONFIG", str(env_cfg))
    assert cli.main(["serve", "--host", "127.0.0.1", "--port", "0"]) == 0
    assert seen[0].server.host == "127.0.0.1" and seen[0].server.port == 0
    monkeypatch.delenv("CCW_CONFIG")
    assert cli.main(["serve"]) == 78  # falls back to /etc/ccw/config.yaml, absent


def test_serve_override_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []

    async def fake(cfg, **kw):
        seen.append(cfg)

    monkeypatch.setattr(cli, "run_service", fake)
    assert (
        cli.main(
            ["serve", "--config", str(write_cfg(tmp_path)), "--host", "0.0.0.0", "--port", "9"]
        )
        == 0
    )
    assert (seen[0].server.host, seen[0].server.port) == ("0.0.0.0", 9)


def test_serve_tooling_error_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    async def fake(cfg, **kw):
        raise ToolingError("mise broke")

    monkeypatch.setattr(cli, "run_service", fake)
    assert cli.main(["serve", "--config", str(write_cfg(tmp_path))]) == 1
    assert "mise broke" in capsys.readouterr().err


def test_python_m_serve_sigterm_clean_exit(tmp_path: Path) -> None:
    cfg = write_cfg(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, "-m", "claude_code_wrapper", "serve", "--config", str(cfg)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    try:
        deadline = time.time() + 20
        started = False
        lines = []
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(line)
            if "listening" in line:
                started = True
                break
        assert started, "".join(lines)
        assert json.loads(lines[-1])["level"]
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=15) == 0
    finally:
        if proc.poll() is None:
            proc.kill()


def test_serve_refuses_empty_token_exit_78(tmp_path: Path, capsys) -> None:
    cfg = write_cfg(
        tmp_path,
        f"""
server: {{host: 127.0.0.1, port: 0, internal_port: 0}}
paths: {{workdir_root: {tmp_path}/wd, sessions_root: {tmp_path}/ss}}
""",
    )
    assert cli.main(["serve", "--config", str(cfg)]) == 78
    err = capsys.readouterr().err
    assert "server.auth_token" in err and "server.allow_anonymous" in err
