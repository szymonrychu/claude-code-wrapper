---
title: Container
status: todo
repo: claude-code-wrapper
value: docker compose up gives a healthy non-root service with persistent volumes
files: [Dockerfile, docker-compose.yml, .dockerignore, .env.example, tests/test_container.py]
verify: "uv run pytest tests/test_container.py -q && docker compose config -q && mise run lint"
depends_on: [service-main]
owns: [container-image]
uses: [service-cli]
model: ""
estimate: 75
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Container

## Requirement

Ship the container (D10): `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.env.example`, `tests/test_container.py`.

Dockerfile (multi-stage):
- builder: `python:3.12-slim-bookworm` + uv (pinned `COPY --from=ghcr.io/astral-sh/uv:<version>`); `uv sync --frozen --no-dev` of the project into `/app/.venv`.
- runtime: `python:3.12-slim-bookworm`; apt `bash git openssh-client jq curl ca-certificates build-essential shellcheck gnupg`; node 24 (pinned, from the official node image or nodesource); `npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}` with `ARG CLAUDE_CODE_VERSION=2.1.304` in its own layer; mise pinned (`COPY --from=jdxcode/mise:<version>` or the install script with `MISE_VERSION`), `ENV MISE_DATA_DIR=/opt/mise MISE_TRUSTED_CONFIG_PATHS=/workdir` and `/opt/mise/shims` on PATH; uv; ruff (`uv tool install` into a system path, or pip); gh and glab (pinned release binaries); user `agent` uid/gid 1000 with `HOME=/home/agent`; dirs `/workdir`, `/sessions`, `/opt/mise`, `/etc/ccw` owned by agent; `ENV DISABLE_AUTOUPDATER=1 PATH=/app/.venv/bin:...`; `COPY config.example.yaml /app/config.example.yaml`; `USER agent`; `WORKDIR /app`; `EXPOSE 8080`; `HEALTHCHECK` curling `/healthz`; `CMD ["ccw", "serve", "--config", "/etc/ccw/config.yaml"]`. Every pinned version has a comment saying why it is pinned.
- docker-compose.yml: service `ccw` (`build: .`, `image: claude-code-wrapper:local`), ports `${CCW_PORT:-8080}:8080`, `environment` passthrough of `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, `CCW_AUTH_TOKEN`, `GH_TOKEN`, `GITLAB_TOKEN` (each `${VAR:-}`), volumes `workdir:/workdir`, `sessions:/sessions`, `mise:/opt/mise`, `${CCW_CONFIG:-./config.example.yaml}:/etc/ccw/config.yaml:ro`; `restart: unless-stopped`; `stop_grace_period: 30s`; top-level named volumes `workdir`, `sessions`, `mise`.
- `.dockerignore`: `.venv`, `.git`, caches, `docs/plans`, `.claude/worktrees`, `.worktrees`.
- `.env.example`: every compose variable with an empty or example value and a comment.
- tests/test_container.py (static, no docker needed): Dockerfile has `USER agent`, a non-root uid, a pinned `CLAUDE_CODE_VERSION` (not `latest`), each required tool, the CMD; compose has the three named volumes mounted at `/workdir`, `/sessions`, `/opt/mise`, the config mount and the env passthrough.

## Acceptance conditions

- AC1: The Dockerfile pins claude, runs as non-root agent and serves ccw [evidence: test:tests/test_container.py::test_dockerfile_non_root_pinned_and_cmd]
- AC2: The Dockerfile installs every tool the workbench plugin needs [evidence: test:tests/test_container.py::test_dockerfile_installs_required_tools]
- AC3: Compose mounts workdir, sessions and mise volumes and the config file [evidence: test:tests/test_container.py::test_compose_volumes_and_config_mount]
- AC4: Compose passes the auth variables through [evidence: test:tests/test_container.py::test_compose_env_passthrough]
- AC5: The compose file is valid [evidence: command:docker compose config -q]
- AC6: The image builds [evidence: command:docker build -t claude-code-wrapper:test .]
- AC7: The image runs as a non-root user [evidence: command:test "$(docker run --rm claude-code-wrapper:test id -u)" != "0"]
- AC8: Every required binary is on PATH in the image [evidence: command:docker run --rm claude-code-wrapper:test bash -lc 'for b in bash git ssh jq node npm claude mise uv ruff gh glab python3 shellcheck ccw ccw-hook; do command -v $b >/dev/null || { echo missing $b; exit 1; }; done']
- AC9: The pinned claude CLI supports the flags the service uses [evidence: command:docker run --rm claude-code-wrapper:test bash -lc 'claude --help | grep -q -- --session-id && claude --help | grep -q -- --resume && claude plugin install --help | grep -q -- --yes']
- AC10: The bundled example config validates inside the image [evidence: command:docker run --rm claude-code-wrapper:test ccw check-config --config /app/config.example.yaml]
- AC11: docker compose up serves /healthz [evidence: command:CCW_PORT=18080 docker compose -p ccw-smoke up -d --build && curl -fsS --retry 30 --retry-connrefused --retry-delay 1 http://127.0.0.1:18080/healthz; rc=$?; docker compose -p ccw-smoke down -v; exit $rc]

## Files

- `Dockerfile`
- `docker-compose.yml`
- `.dockerignore`
- `.env.example`
- `tests/test_container.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
