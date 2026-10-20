"""Packaging: build the wheel, install it in a fresh venv, exercise the installed commands."""

from __future__ import annotations

import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

from claude_code_wrapper.config import load_config

ROOT = Path(__file__).resolve().parent.parent
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    base = tmp_path_factory.mktemp("pkg")
    dist = base / "dist"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist), str(ROOT)],
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(dist.glob("claude_code_wrapper-*.whl"))
    venv = base / "venv"
    subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(venv)], check=True, capture_output=True
    )
    subprocess.run(
        ["uv", "pip", "install", "--python", str(venv / "bin" / "python"), str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )
    return wheel, venv / "bin"


def _run(bindir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(bindir / args[0]), *args[1:]],
        capture_output=True,
        text=True,
        cwd=bindir.parent,
        check=False,
    )


def test_wheel_installs_and_ccw_version(installed):
    _, bindir = installed
    res = _run(bindir, "ccw", "--version")
    assert res.returncode == 0, res.stderr
    assert VERSION in res.stdout


def test_config_example_from_installed_wheel(installed, tmp_path):
    _, bindir = installed
    res = _run(bindir, "ccw", "config", "example")
    assert res.returncode == 0, res.stderr
    assert res.stdout == (ROOT / "config.example.yaml").read_text()
    path = tmp_path / "config.yaml"
    path.write_text(res.stdout)
    assert load_config(path).server.port == 8080


def test_hook_relay_installed_and_referenced(installed):
    _, bindir = installed
    assert (bindir / "ccw-hook").is_file()
    res = _run(
        bindir,
        "python",
        "-c",
        "from claude_code_wrapper.bootstrap.settings import default_relay_command as d;print(d())",
    )
    assert Path(res.stdout.strip()) == bindir / "ccw-hook"


def test_wheel_contains_data_files(installed):
    wheel, _ = installed
    names = zipfile.ZipFile(wheel).namelist()
    assert "claude_code_wrapper/data/config.example.yaml" in names
    assert "claude_code_wrapper/data/examples/workbench.config.yaml" in names
    assert "claude_code_wrapper/hook_relay.py" in names


def test_serve_preflight_missing_binaries(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("claude:\n  binary: definitely-not-claude-xyz\n")
    res = subprocess.run(
        [sys.executable, "-m", "claude_code_wrapper", "serve", "--config", str(cfg)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert res.returncode != 0
    assert "definitely-not-claude-xyz" in res.stderr
    assert "PATH" in res.stderr


def test_example_config_has_no_hardcoded_container_paths():
    text = (ROOT / "config.example.yaml").read_text()
    stripped = text.replace("${CCW_SESSIONS_ROOT:-/sessions}", "")
    for line in stripped.splitlines():
        assert "/sessions/" not in line, line


def test_dockerfile_installs_wheel_via_pip():
    text = (ROOT / "Dockerfile").read_text()
    assert "uv build" in text or "pip wheel" in text
    assert "pip install" in text and ".whl" in text
    assert 'CMD ["ccw", "serve"]' in text
    assert "COPY .venv" not in text and "COPY --from=builder /app/.venv" not in text
    assert "COPY src" in text.split("FROM", 2)[1] or "COPY src" in text  # source only in builder
    runtime = text.rsplit("\nFROM ", 1)[1]
    assert "COPY src" not in runtime


def test_readme_installation_section():
    text = (ROOT / "README.md").read_text()
    assert "## Installation" in text
    section = text.split("## Installation", 1)[1].split("\n## ", 1)[0]
    for needle in (
        "pip install",
        "pipx",
        "uv tool install",
        "git",
        "claude",
        "mise",
        "ccw config example",
        "ccw serve --config",
    ):
        assert needle in section, needle
