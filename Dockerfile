# syntax=docker/dockerfile:1

# ---- builder: build the wheel and its dependency wheels; the runtime pip-installs them ----
FROM python:3.12-slim-bookworm AS builder
# uv pinned so the build is reproducible.
COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /uvx /bin/
ENV UV_PYTHON_DOWNLOADS=never
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN uv build --wheel --out-dir /dist

# ---- node source: official image, copied so node is exact and not an apt guess ----
# Node 24 pinned: Claude Code requires a current LTS line; bump deliberately with the CLI.
FROM node:24.22.0-bookworm-slim AS node

# ---- runtime ----
FROM python:3.12-slim-bookworm AS runtime

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

# Install the service from the wheel: ccw and ccw-hook land on PATH, no source tree or .venv.
RUN --mount=type=bind,from=builder,source=/dist,target=/tmp/dist \
    pip install --no-cache-dir /tmp/dist/*.whl

# Shims on PATH via image ENV so mise tools resolve in Bash-tool shells; data dir sits on
# the `mise` volume, outside HOME.
ENV HOME=/home/agent \
    MISE_DATA_DIR=/opt/mise \
    MISE_TRUSTED_CONFIG_PATHS=/workdir \
    DISABLE_AUTOUPDATER=1 \
    CCW_CONFIG=/etc/ccw/config.yaml \
    PATH=/opt/mise/shims:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

# pip put ccw and ccw-hook in /usr/local/bin, which Debian's /etc/profile keeps in login shells;
# the mise shims need re-adding there.
RUN printf 'export PATH=/opt/mise/shims:$PATH\n' > /etc/profile.d/mise-shims.sh

USER agent
WORKDIR /home/agent
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --start-interval=1s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1
CMD ["ccw", "serve"]
