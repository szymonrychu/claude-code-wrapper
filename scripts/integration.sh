#!/usr/bin/env bash
# Integration tests against the real container (docker compose + scripted fake claude).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

export CCW_PORT="${CCW_PORT:-8080}"
export CCW_BASE_URL="http://127.0.0.1:${CCW_PORT}"
export CCW_AUTH_TOKEN="ci-token"
export CCW_INTEGRATION_TASK=1
HEALTH_TIMEOUT="${CCW_HEALTH_TIMEOUT:-120}"

compose() {
  docker compose -p ccw-it -f docker-compose.yml -f docker-compose.ci.yml "$@"
}

status=1
cleanup() {
  if [ "$status" -ne 0 ]; then
    compose logs --no-color || true
  fi
  compose down -v --remove-orphans || true
}
trap 'status=$?; cleanup; exit $status' EXIT

wait_for() {
  local path="$1" deadline=$((SECONDS + HEALTH_TIMEOUT))
  until curl -fsS -o /dev/null "${CCW_BASE_URL}${path}" 2>/dev/null; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "timed out after ${HEALTH_TIMEOUT}s waiting for ${CCW_BASE_URL}${path}" >&2
      return 1
    fi
    sleep 1
  done
}

compose up -d --build ccw
wait_for /healthz
wait_for /readyz
uv run pytest tests/integration -m integration -p no:cacheprovider
status=0
