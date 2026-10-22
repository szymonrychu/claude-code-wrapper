#!/usr/bin/env bash
# Two-container peer integration tests (docker compose + scripted fake claude).
# CCW_SKIP_BUILD=1 reuses an already built claude-code-wrapper:local image.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

export CCW_PEER_A_PORT="${CCW_PEER_A_PORT:-8181}"
export CCW_PEER_B_PORT="${CCW_PEER_B_PORT:-8182}"
export CCW_PEER_A_URL="http://127.0.0.1:${CCW_PEER_A_PORT}"
export CCW_PEER_B_URL="http://127.0.0.1:${CCW_PEER_B_PORT}"
export CCW_AUTH_TOKEN="ci-token"
export CCW_PEER_TOKEN="ci-peer-token-0123456789"
export CCW_INTEGRATION_TASK=1
export CCW_PEERS_COMPOSE_ARGS="-p ccw-it-peers -f examples/docker-compose.peers.yml -f docker-compose.peers-ci.yml"
HEALTH_TIMEOUT="${CCW_HEALTH_TIMEOUT:-120}"

compose() {
  # shellcheck disable=SC2086  # the args string is deliberately word-split
  docker compose $CCW_PEERS_COMPOSE_ARGS "$@"
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
  local url="$1" deadline=$((SECONDS + HEALTH_TIMEOUT))
  until curl -fsS -o /dev/null "$url" 2>/dev/null; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "timed out after ${HEALTH_TIMEOUT}s waiting for ${url}" >&2
      return 1
    fi
    sleep 1
  done
}

if [ "${CCW_SKIP_BUILD:-0}" != "1" ]; then
  compose build ccw-a
fi
compose up -d
for base in "$CCW_PEER_A_URL" "$CCW_PEER_B_URL"; do
  wait_for "${base}/healthz"
  wait_for "${base}/readyz"
done
uv run pytest tests/integration_peers -m integration -p no:cacheprovider
status=0
