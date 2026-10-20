---
title: Container
status: in-progress
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
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/claude-code-wrapper-microser--container
branch: claude-code-wrapper-microser--container
verified_head: ""
run: claude-code-wrapper-microser
provides: []
acs_for_audit: []
process_gaps: []
---

# Container

## Requirement

Ship the container (D10): `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.env.example`, `tests/test_container.py`.

Dockerfile (multi-stage):
- builder: `python:3.12-slim-bookworm` + uv (pinned `COPY --from=ghcr.io/astral-sh/uv:<version>`); `uv sync --frozen --no-dev` of the project into `/app/.venv`.
- runtime: `python:3.12-slim-bookworm`; apt `bash git openssh-client jq curl ca-certificates build-essential shellcheck gnupg`; node 24 (pinned, from the official node image or nodesource); `npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}` with `ARG CLAUDE_CODE_VERSION=2.1.304` in its own layer; mise pinned (`COPY --from=jdxcode/mise:<version>` or the install script with `MISE_VERSION`), `ENV MISE_DATA_DIR=/opt/mise MISE_TRUSTED_CONFIG_PATHS=/workdir` and `/opt/mise/shims` on PATH; uv; ruff (`uv tool install` into a system path, or pip); gh and glab (pinned release binaries); user `agent` uid/gid 1000 with `HOME=/home/agent`; dirs `/workdir`, `/sessions`, `/opt/mise`, `/etc/ccw` owned by agent; `ENV DISABLE_AUTOUPDATER=1 PATH=/app/.venv/bin:...`; `COPY config.example.yaml /app/config.example.yaml`; `USER agent`; `WORKDIR /app`; `EXPOSE 8080`; `HEALTHCHECK` curling `/healthz`; `ENV CCW_CONFIG=/etc/ccw/config.yaml`; `CMD ["ccw", "serve"]`. Every pinned version has a comment saying why it is pinned.
- docker-compose.yml: service `ccw` (`build: .`, `image: claude-code-wrapper:local`), ports `${CCW_PORT:-8080}:8080`, `environment` passthrough of `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, `CCW_AUTH_TOKEN`, `GH_TOKEN`, `GITLAB_TOKEN` (each `${VAR:-}`), volumes `workdir:/workdir`, `sessions:/sessions`, `mise:/opt/mise`, `${CCW_CONFIG_FILE:-./config.example.yaml}:/etc/ccw/config.yaml:ro`; `restart: unless-stopped`; `stop_grace_period: 30s`; top-level named volumes `workdir`, `sessions`, `mise`.
- `.dockerignore`: `.venv`, `.git`, caches, `docs/plans`, `.claude/worktrees`, `.worktrees`.
- `.env.example`: every compose variable with an empty or example value and a comment.
- tests/test_container.py (static, no docker needed): Dockerfile has `USER agent`, a non-root uid, a pinned `CLAUDE_CODE_VERSION` (not `latest`), each required tool, the CMD; compose has the three named volumes mounted at `/workdir`, `/sessions`, `/opt/mise`, the config mount and the env passthrough.

Settled by the builder check (binding):
- Docker 29.8.2 is available on the build machine; every docker command AC is mandatory.

Settled at the gate (D19-D24, binding):
- Compose declares the named volume `mise` at `/opt/mise` (D21); the host config file var is `CCW_CONFIG_FILE`; the image sets `CCW_CONFIG=/etc/ccw/config.yaml` and runs `ccw serve`; `stop_grace_period` exceeds `limits.shutdown_timeout_seconds`.

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

- AC12: The image sets CCW_CONFIG and runs ccw serve without a config flag [evidence: test:tests/test_container.py::test_dockerfile_ccw_config_env]

## Files

- `Dockerfile`
- `docker-compose.yml`
- `.dockerignore`
- `.env.example`
- `tests/test_container.py`

## Interfaces

- Part `container-image`: `Dockerfile` and `docker-compose.yml` (service `ccw`, volumes `workdir`, `sessions`, `mise`, config at `/etc/ccw/config.yaml`). Consumers: workbench-example (compose override), readme-config (docs).

## Expected outcome

`docker compose up` gives a running, healthy, non-root service with persistent workdirs, sessions and tools.

## Steps

- Write `tests/test_container.py`; red; write Dockerfile, compose, .dockerignore, .env.example; green.
- Run the docker command ACs in order (config, build, run checks, compose smoke); fix until each exits 0; `mise run lint`.

## Notes

- claude refuses bypassPermissions as root (magic `Dockerfile.agent:31-47`); tatara runs uid 10001, magic uid 1000.
- mise in Bash-tool shells needs shims on PATH via image ENV (tatara pitfall); keep MISE_DATA_DIR on the `mise` volume, outside HOME.
- The compose smoke uses project `ccw-smoke` and port 18080 so it never touches a user's running stack; `down -v` removes only that project's volumes.

- 2026-10-20 run claude-code-wrapper-microser: dependency-failed on service-main
