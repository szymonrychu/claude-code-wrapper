"""Static checks of the container files (no docker needed)."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text()
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def test_dockerfile_runs_as_non_root_agent():
    assert re.search(r"^USER agent$", DOCKERFILE, re.M)
    assert re.search(r"useradd[^\n]*(-u|--uid)\s*1000", DOCKERFILE)
    assert re.search(r"groupadd[^\n]*(-g|--gid)\s*1000", DOCKERFILE)
    assert "HOME=/home/agent" in DOCKERFILE


def test_dockerfile_pins_claude_code():
    m = re.search(r"^ARG CLAUDE_CODE_VERSION=(\S+)$", DOCKERFILE, re.M)
    assert m and m.group(1) == "2.1.304"
    assert "latest" not in m.group(1)
    assert "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" in DOCKERFILE
    assert "DISABLE_AUTOUPDATER=1" in DOCKERFILE


def test_dockerfile_has_required_tools():
    for tool in (
        "bash",
        "git",
        "openssh-client",
        "jq",
        "curl",
        "ca-certificates",
        "build-essential",
        "shellcheck",
        "gnupg",
    ):
        assert re.search(rf"\b{re.escape(tool)}\b", DOCKERFILE), tool
    for needle in ("node", "mise", "ruff", "uv", "gh", "glab"):
        assert re.search(rf"\b{needle}\b", DOCKERFILE), needle
    assert "ghcr.io/astral-sh/uv:" in DOCKERFILE
    assert "MISE_DATA_DIR=/opt/mise" in DOCKERFILE
    assert "MISE_TRUSTED_CONFIG_PATHS=/workdir" in DOCKERFILE
    assert "/opt/mise/shims" in DOCKERFILE
    assert "/app/.venv/bin" in DOCKERFILE


def test_dockerfile_runtime_shape():
    assert "FROM python:3.12-slim-trixie AS builder" in DOCKERFILE
    assert "uv sync --frozen --no-dev" in DOCKERFILE
    assert "COPY config.example.yaml /app/config.example.yaml" in DOCKERFILE
    for d in ("/workdir", "/sessions", "/opt/mise", "/etc/ccw"):
        assert d in DOCKERFILE
    assert "CCW_CONFIG=/etc/ccw/config.yaml" in DOCKERFILE
    assert "EXPOSE 8080" in DOCKERFILE
    assert re.search(r"HEALTHCHECK[^\n]*(\\\n[^\n]*)*/healthz", DOCKERFILE)
    assert re.search(r'^CMD \["ccw", "serve"\]$', DOCKERFILE, re.M)
    assert DOCKERFILE.rstrip().splitlines()[-1].startswith("CMD")


def test_dockerfile_pins_have_reasons():
    pins = [ln for ln in DOCKERFILE.splitlines() if re.search(r"^(ARG|ENV) \w*VERSION=", ln)]
    assert len(pins) >= 4
    lines = DOCKERFILE.splitlines()
    for ln in pins:
        i = lines.index(ln)
        assert lines[i - 1].lstrip().startswith("#"), f"unexplained pin: {ln}"


def test_compose_service_and_ports():
    svc = COMPOSE["services"]["ccw"]
    assert svc["build"] == "."
    assert svc["image"] == "claude-code-wrapper:local"
    assert "${CCW_PORT:-8080}:8080" in svc["ports"]
    assert svc["restart"] == "unless-stopped"
    assert svc["stop_grace_period"] == "30s"


def test_compose_volumes():
    svc = COMPOSE["services"]["ccw"]
    assert set(COMPOSE["volumes"]) == {"workdir", "sessions", "mise"}
    assert "workdir:/workdir" in svc["volumes"]
    assert "sessions:/sessions" in svc["volumes"]
    assert "mise:/opt/mise" in svc["volumes"]
    assert "${CCW_CONFIG_FILE:-./config.example.yaml}:/etc/ccw/config.yaml:ro" in svc["volumes"]


def test_compose_env_passthrough_and_env_example():
    env = COMPOSE["services"]["ccw"]["environment"]
    names = [
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "CCW_AUTH_TOKEN",
        "GH_TOKEN",
        "GITLAB_TOKEN",
    ]
    for n in names:
        assert env[n] == f"${{{n}:-}}"
    example = (ROOT / ".env.example").read_text()
    for n in [*names, "CCW_PORT", "CCW_CONFIG_FILE"]:
        assert re.search(rf"^#?\s*{n}=", example, re.M), n


def test_shutdown_grace_exceeds_config_timeout():
    cfg = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    assert cfg["limits"]["shutdown_timeout_seconds"] < 30


def test_dockerignore_excludes_noise():
    ignore = (ROOT / ".dockerignore").read_text().split()
    for p in (".venv", ".git", "docs/plans", ".claude/worktrees", ".worktrees"):
        assert p in ignore, p
