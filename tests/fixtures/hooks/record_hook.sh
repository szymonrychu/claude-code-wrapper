#!/bin/sh
# e2e user hook: append {"event": $CCW_EVENT, "payload": <stdin JSON>} as one line to
# $RECORD_HOOK_LOG. onAgentToolCall for the Write tool is denied: exit 2, reason on stderr.
set -eu
payload=$(cat)
[ -n "$payload" ] || payload=null
# Build the line in a temp file and append it with one write, so concurrent hooks never
# interleave their lines.
line=$(mktemp "${TMPDIR:-/tmp}/record_hook.XXXXXX")
trap 'rm -f "$line"' EXIT
printf '{"event": "%s", "payload": %s}\n' "${CCW_EVENT:-}" "$payload" > "$line"
cat "$line" >> "$RECORD_HOOK_LOG"
if [ "${CCW_EVENT:-}" = "onAgentToolCall" ] &&
    printf '%s' "$payload" | grep -Eq '"tool_name": ?"Write"'; then
  echo "record_hook: the Write tool is denied in e2e" >&2
  exit 2
fi
exit 0
