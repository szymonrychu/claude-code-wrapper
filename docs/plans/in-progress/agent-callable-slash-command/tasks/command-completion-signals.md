---
title: Command completion signals
status: in-progress
repo: .
value: "ccw knows reliably when a typed slash command finished, and reports every compaction including Claude Code's own"
files: [src/claude_code_wrapper/command_signals.py, src/claude_code_wrapper/router.py, src/claude_code_wrapper/hook_relay.py, README.md, tests/test_command_signals.py, tests/test_hook_relay.py]
verify: "uv run pytest tests/test_command_signals.py tests/test_hook_relay.py tests/test_bootstrap_settings.py tests/test_internal_api.py tests/test_router.py tests/test_readme_api.py -q && mise run lint"
depends_on: [fake-claude-slash-commands, transcript-command-entries, command-event-vocabulary]
owns: [command-signals, compact-relay-events]
uses: [fake-claude-commands, transcript-command-events, command-events, command-metrics]
model: sonnet
estimate: 75
actual: ""
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/agent-callable-slash-command--command-completion-signals
branch: agent-callable-slash-command--command-completion-signals
verified_head: ""
run: ""
provides: []
---

# Command completion signals

## Requirement
Completion signals for commands and the `compacted` event (design D4 step 3 signal sources, D7). Owns parts `command-signals` (C/command_signals.py) and `compact-relay-events` (C/hook_relay.py).

- `hook_relay.CLAUDE_HOOK_EVENTS` gains `PreCompact` and `PostCompact` (appended); `render_settings` installs relay groups for them through its existing loop; the internal API accepts them; tests/test_hook_relay.py::test_constants pins the new tuple.
- `command_signals.py` (new): `Signal(kind, data, seq)` with kinds `pre_compact`, `post_compact`, `compact_boundary`, `command_invoked`, `command_output`; `SignalBus` with `publish(kind, data) -> Signal`, `mark() -> int` (the current seq) and `async wait(predicate, *, after: int, timeout: float) -> Signal | None` returning the first signal with seq > after that matches, already published ones included, so a signal published between typing and waiting is never missed; None on timeout; bounded history (last 64 signals).
- `router.py` (owns a `SignalBus` as `self.signals`):
  - `PreCompact` relay: answer allow (no user hook), publish `pre_compact {trigger, custom_instructions}`;
  - `PostCompact` relay: answer allow, publish `post_compact {trigger, compact_summary}`;
  - transcript `CompactBoundary` on the main chain: emit `compacted {trigger, pre_tokens, post_tokens, duration_ms}`, count `Metrics.compaction(trigger)`, publish `compact_boundary` with the same data;
  - transcript `CommandInvoked` / `CommandOutput` on the main chain: publish `command_invoked {name, args}` / `command_output {stdout, stderr}`;
  - sidechain (subagent) versions publish nothing and emit nothing;
  - `compacted` comes only from the boundary, so one compaction is one event even when PostCompact also arrives; Claude Code's own compaction (trigger auto) is reported the same way.
- README: `### POST /internal/hooks/{event}` lists PreCompact and PostCompact; `### compacted` says it covers manual and auto compactions.

## Acceptance conditions

- AC1: CLAUDE_HOOK_EVENTS ends with PreCompact and PostCompact [evidence: test:tests/test_hook_relay.py::test_constants]
- AC2: render_settings installs relay groups for PreCompact and PostCompact and the internal API accepts them [evidence: test:tests/test_command_signals.py::test_compact_relays_installed_and_accepted]
- AC3: a compact_boundary transcript line emits one compacted event with its metadata and counts the metric [evidence: test:tests/test_command_signals.py::test_boundary_emits_compacted_event]
- AC4: Claude Code's own auto compaction in a running turn is reported as compacted with trigger auto [evidence: test:tests/test_command_signals.py::test_auto_compaction_reported]
- AC5: PreCompact and PostCompact relays answer allow and publish their signals [evidence: test:tests/test_command_signals.py::test_compact_relays_publish_signals]
- AC6: command echo and output transcript lines publish command_invoked and command_output [evidence: test:tests/test_command_signals.py::test_command_lines_publish_signals]
- AC7: wait after a mark returns a signal published before the wait started [evidence: test:tests/test_command_signals.py::test_signal_bus_wait_after_mark]
- AC8: wait returns None on timeout and history stays bounded [evidence: test:tests/test_command_signals.py::test_signal_bus_timeout_and_bound]
- AC9: sidechain boundary and command lines publish and emit nothing [evidence: test:tests/test_command_signals.py::test_sidechain_lines_ignored]
- AC10: existing relay, settings and internal API tests stay green [evidence: command:uv run pytest tests/test_hook_relay.py tests/test_bootstrap_settings.py tests/test_internal_api.py tests/test_router.py tests/test_readme_api.py -q]

## Files

- `src/claude_code_wrapper/command_signals.py`
- `src/claude_code_wrapper/router.py`
- `src/claude_code_wrapper/hook_relay.py`
- `README.md`
- `tests/test_command_signals.py`
- `tests/test_hook_relay.py`

## Interfaces

`command_signals.Signal`, `command_signals.SignalBus.publish/mark/wait`, `SessionRouter.signals`; `CLAUDE_HOOK_EVENTS` with PreCompact and PostCompact.

## Expected outcome

PreCompact and PostCompact reach ccw through the relay, every main-chain compaction yields one `compacted` event and metric, and the router publishes completion signals that a waiter started after a mark can never miss.

## Steps

- Update tests/test_hook_relay.py::test_constants and write tests/test_command_signals.py (SignalBus unit tests, router tests fed with relay payloads and transcript lines, a World test with the fake's auto_compact step); run them and see them fail.
- Add command_signals.py, the relay events, the router handlers and the README lines.
- Run `uv run pytest tests/test_command_signals.py tests/test_hook_relay.py tests/test_bootstrap_settings.py tests/test_internal_api.py tests/test_router.py tests/test_readme_api.py -q && mise run lint`.
- A retirement test, if any, asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Uses the fake's `auto_compact` step and `/compact` (part `fake-claude-commands`) for the world tests and the transcript events of task transcript-command-entries. A world test can type `/compact` itself with the session's PTY (`session.pty.send_prompt`) at idle; the runner that does it for real is task command-runner.
