#!/usr/bin/env bash
# live-claude: the built image runs the real Claude Code it ships (Dockerfile
# CLAUDE_CODE_VERSION) against tests/live/fake_messages_api.py and the managed-settings baseline.
# Builds the image unless CCW_SKIP_BUILD=1. No real credentials: the API key is a dummy.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

IMAGE="${CCW_IMAGE:-claude-code-wrapper:local}"
CONTAINER=ccw-live
export CCW_PORT="${CCW_PORT:-8080}"
export CCW_BASE_URL="http://127.0.0.1:${CCW_PORT}"
export LIVE_API_PORT="${LIVE_API_PORT:-18765}"
export LIVE_EXEC="docker exec ${CONTAINER}"
export LIVE_TASK=1
HEALTH_TIMEOUT="${CCW_HEALTH_TIMEOUT:-120}"

LIVE_DIR="$(mktemp -d)"
export LIVE_API_LOG="${LIVE_DIR}/api.jsonl"
api_pid=""

status=1
cleanup() {
  if [ "$status" -ne 0 ]; then
    echo "---- ${CONTAINER} logs ----" >&2
    docker logs "$CONTAINER" >&2 || true
    echo "---- fake Messages API: requests (path, case, reply kind) ----" >&2
    if [ -f "$LIVE_API_LOG" ]; then
      jq -c '[.path, .case, .reply[0]]' "$LIVE_API_LOG" >&2 || true
    fi
  fi
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  if [ -n "$api_pid" ]; then
    kill "$api_pid" 2>/dev/null || true
  fi
  rm -rf "$LIVE_DIR"
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

if [ "${CCW_SKIP_BUILD:-0}" != "1" ]; then
  docker build -t "$IMAGE" .
fi

uv run python tests/live/fake_messages_api.py "$LIVE_API_PORT" "$LIVE_API_LOG" 127.0.0.1 &
api_pid=$!

# Host network: the container reaches the fake API on the runner's loopback.
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" --network host \
  -e CCW_PORT -e LIVE_API_PORT -e LIVE_API_KEY=CANARY-live-dummy-key \
  -v "$PWD/tests/live/config.live.yaml:/etc/ccw/config.yaml:ro" \
  -v "$PWD/tests/live/mods:/opt/live-mods:ro" \
  "$IMAGE" >/dev/null

wait_for /healthz
wait_for /readyz
uv run pytest -m live tests/live -p no:cacheprovider -rA
status=0
