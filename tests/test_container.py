"""Static checks of the container files (no docker needed)."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text()
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def test_dockerfile_non_root_pinned_and_cmd():
    assert re.search(r"^USER agent$", DOCKERFILE, re.M)
    assert re.search(r"useradd[^\n]*(-u|--uid)\s*1000", DOCKERFILE)
    assert re.search(r"groupadd[^\n]*(-g|--gid)\s*1000", DOCKERFILE)
    assert "HOME=/home/agent" in DOCKERFILE
    m = re.search(r"^ARG CLAUDE_CODE_VERSION=(\S+)$", DOCKERFILE, re.M)
    assert m and m.group(1) == "2.1.304"
    assert "latest" not in m.group(1)
    assert "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" in DOCKERFILE
    assert "DISABLE_AUTOUPDATER=1" in DOCKERFILE
    assert re.search(r'^CMD \["ccw", "serve"\]$', DOCKERFILE, re.M)
    assert DOCKERFILE.rstrip().splitlines()[-1].startswith("CMD")


def test_dockerfile_installs_required_tools():
    apt = re.search(r"apt-get install[^\n]*\\\n\s*([^\n]*?)\s*\\\n", DOCKERFILE)
    assert apt, "apt-get install line missing"
    packages = apt.group(1).split()
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
        assert tool in packages, tool
    code = "\n".join(ln for ln in DOCKERFILE.splitlines() if not ln.lstrip().startswith("#"))
    assert re.search(r"^FROM node:24\.[\w.]+-bookworm-slim AS node$", code, re.M)
    assert "COPY --from=node /usr/local/bin/node " in code
    assert re.search(r"^RUN npm install -g @anthropic-ai/claude-code@", code, re.M)
    uv_copy = r"^COPY --from=ghcr\.io/astral-sh/uv:\d[\w.]* /uv /uvx /usr/local/bin/"
    assert re.search(uv_copy, code, re.M)
    assert re.search(r"^COPY --from=jdxcode/mise:[\w.]+ /usr/local/bin/mise ", code, re.M)
    assert re.search(r"uv tool install ruff==\$\{RUFF_VERSION\}", code)
    assert re.search(r"github\.com/cli/cli/releases/download/v\$\{GH_VERSION\}", code)
    assert re.search(r"gitlab-org/cli/-/releases/v\$\{GLAB_VERSION\}", code)
    assert "MISE_DATA_DIR=/opt/mise" in code
    assert "MISE_TRUSTED_CONFIG_PATHS=/workdir" in code
    path = re.search(r"PATH=(\S+)", code)
    assert path
    assert "/opt/mise/shims" in path.group(1)
    # Debian's /etc/profile resets PATH for login shells: pip puts ccw in /usr/local/bin, which
    # survives; the mise shims are re-added.
    assert "/usr/local/bin" in path.group(1)
    assert "/etc/profile.d/" in code


def test_dockerfile_ccw_config_env():
    assert "CCW_CONFIG=/etc/ccw/config.yaml" in DOCKERFILE
    assert re.search(r'^CMD \["ccw", "serve"\]$', DOCKERFILE, re.M)
    assert "--config" not in DOCKERFILE.split("CMD", 1)[1]


def test_dockerfile_runtime_shape():
    assert "FROM python:3.12-slim-bookworm AS builder" in DOCKERFILE
    assert "FROM python:3.12-slim-bookworm AS runtime" in DOCKERFILE
    assert "trixie" not in DOCKERFILE
    assert "uv build --wheel" in DOCKERFILE
    assert "pip install --no-cache-dir /tmp/dist/*.whl" in DOCKERFILE
    # no source tree or venv in the image; /app only holds the generated example config
    stripped = DOCKERFILE.replace("/app/config.example.yaml", "").replace("mkdir -p /app", "")
    stripped = stripped.replace("chown -R agent:agent /app", "")
    assert not re.search(r"^(?:COPY|WORKDIR|ENV|RUN).*(?:/app\b|\.venv)", stripped, re.M)
    for d in ("/workdir", "/sessions", "/opt/mise", "/etc/ccw"):
        assert d in DOCKERFILE
    assert "EXPOSE 8080" in DOCKERFILE
    assert re.search(r"HEALTHCHECK[^\n]*(\\\n[^\n]*)*/healthz", DOCKERFILE)


def test_dockerfile_pins_have_reasons():
    pins = [ln for ln in DOCKERFILE.splitlines() if re.search(r"^(ARG|ENV) \w*VERSION=", ln)]
    assert len(pins) >= 4
    lines = DOCKERFILE.splitlines()
    for ln in pins:
        i = lines.index(ln)
        assert lines[i - 1].lstrip().startswith("#"), f"unexplained pin: {ln}"


def test_compose_waits_for_health_on_up():
    ready = COMPOSE["services"]["ccw-ready"]
    assert ready["depends_on"]["ccw"]["condition"] == "service_healthy"


def test_compose_service_and_ports():
    svc = COMPOSE["services"]["ccw"]
    assert svc["build"] == "."
    assert svc["image"] == "claude-code-wrapper:local"
    assert "${CCW_PORT:-8080}:8080" in svc["ports"]
    assert svc["restart"] == "unless-stopped"
    assert svc["stop_grace_period"] == "30s"


def test_compose_volumes_and_config_mount():
    svc = COMPOSE["services"]["ccw"]
    assert set(COMPOSE["volumes"]) == {"workdir", "sessions", "mise"}
    assert "workdir:/workdir" in svc["volumes"]
    assert "sessions:/sessions" in svc["volumes"]
    assert "mise:/opt/mise" in svc["volumes"]
    assert "${CCW_CONFIG_FILE:-./config.example.yaml}:/etc/ccw/config.yaml:ro" in svc["volumes"]


def test_compose_env_passthrough():
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


def test_env_example_lists_every_compose_variable():
    names = [*COMPOSE["services"]["ccw"]["environment"], "CCW_PORT", "CCW_CONFIG_FILE"]
    example = (ROOT / ".env.example").read_text()
    for n in names:
        assert re.search(rf"^#?\s*{n}=", example, re.M), n


def test_shutdown_grace_exceeds_config_timeout():
    cfg = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    assert cfg["limits"]["shutdown_timeout_seconds"] < 30


def test_dockerignore_excludes_noise():
    ignore = (ROOT / ".dockerignore").read_text().split()
    for p in (".venv", ".git", "docs/plans", ".claude/worktrees", ".worktrees"):
        assert p in ignore, p


def test_image_ships_valid_example_config():
    """AC10: the built image carries /app/config.example.yaml and ccw validates it."""
    import subprocess

    tag = "claude-code-wrapper:test"
    subprocess.run(["docker", "build", "-t", tag, str(ROOT)], check=True, timeout=1800)
    cmd = ["ccw", "check-config", "--config", "/app/config.example.yaml"]
    r = subprocess.run(
        ["docker", "run", "--rm", tag, *cmd],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stdout + r.stderr
