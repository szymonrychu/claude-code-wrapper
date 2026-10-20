# syntax=docker/dockerfile:1

# ---- builder: resolve the project's locked dependencies into /app/.venv ----
# trixie, not bookworm: bookworm apt signing keys are rejected as invalid on current mirrors.
FROM python:3.12-slim-trixie AS builder
# uv pinned so the lockfile resolves identically on every build (0.13.x reads uv.lock v1).
COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# ---- node source: official image, copied so node is exact and not an apt guess ----
# Node 24 pinned: Claude Code requires a current LTS line; bump deliberately with the CLI.
FROM node:24.22.0-trixie-slim AS node

# ---- runtime ----
FROM python:3.12-slim-trixie AS runtime

# Claude Code CLI version pinned: the PTY driver depends on its dialogs and hook behaviour.
ARG CLAUDE_CODE_VERSION=2.1.304
# ruff pinned for a reproducible lint tool available to agent sessions.
ARG RUFF_VERSION=0.15.0
# gh pinned release binary: apt in Debian has no current gh.
ARG GH_VERSION=2.102.0
# glab pinned release binary: not packaged in Debian.
ARG GLAB_VERSION=1.124.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash git openssh-client jq curl ca-certificates build-essential shellcheck gnupg \
 && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# Claude Code in its own layer so a version bump does not invalidate the tooling layers.
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} && npm cache clean --force

# uv and mise pinned (uv matches the builder stage; mise keeps workdir mise.toml resolution reproducible).
COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /uvx /usr/local/bin/
COPY --from=jdxcode/mise:2026.10.15 /usr/local/bin/mise /usr/local/bin/mise
RUN UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin uv tool install ruff==${RUFF_VERSION}

RUN arch="$(dpkg --print-architecture)" \
 && curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch}.tar.gz" \
      | tar -xz -C /tmp \
 && install /tmp/gh_${GH_VERSION}_linux_${arch}/bin/gh /usr/local/bin/gh \
 && curl -fsSL "https://gitlab.com/gitlab-org/cli/-/releases/v${GLAB_VERSION}/downloads/glab_${GLAB_VERSION}_linux_${arch}.tar.gz" \
      | tar -xz -C /tmp bin/glab \
 && install /tmp/bin/glab /usr/local/bin/glab \
 && rm -rf /tmp/gh_* /tmp/bin

# uid/gid 1000: claude refuses bypassPermissions as root.
RUN groupadd --gid 1000 agent \
 && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/agent --shell /bin/bash agent \
 && mkdir -p /workdir /sessions /opt/mise /etc/ccw \
 && chown -R agent:agent /workdir /sessions /opt/mise /etc/ccw /home/agent

COPY --from=builder --chown=agent:agent /app /app
COPY config.example.yaml /app/config.example.yaml

# Shims on PATH via image ENV so mise tools resolve in Bash-tool shells; data dir sits on
# the `mise` volume, outside HOME.
ENV HOME=/home/agent \
    MISE_DATA_DIR=/opt/mise \
    MISE_TRUSTED_CONFIG_PATHS=/workdir \
    DISABLE_AUTOUPDATER=1 \
    CCW_CONFIG=/etc/ccw/config.yaml \
    PATH=/app/.venv/bin:/opt/mise/shims:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

USER agent
WORKDIR /app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1
CMD ["ccw", "serve"]
