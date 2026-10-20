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


def test_version_flag_matches_pyproject(installed):
    _, bindir = installed
    res = _run(bindir, "ccw", "--version")
    assert res.returncode == 0, res.stderr
    assert VERSION in res.stdout


def test_config_example_is_valid_config(installed, tmp_path):
    _, bindir = installed
    res = _run(bindir, "ccw", "config", "example")
    assert res.returncode == 0, res.stderr
    assert res.stdout == (ROOT / "config.example.yaml").read_text()
    path = tmp_path / "config.yaml"
    path.write_text(res.stdout)
    assert load_config(path).server.port == 8080


def test_hook_relay_on_venv_path(installed):
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


def test_serve_fails_clearly_without_claude_and_git(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("claude:\n  binary: definitely-not-claude-xyz\n")
    res = subprocess.run(
        [sys.executable, "-m", "claude_code_wrapper", "serve", "--config", str(cfg)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0
    assert "definitely-not-claude-xyz" in res.stderr
    assert "PATH" in res.stderr
