---
title: PreToolUse gate fails closed
status: todo
repo: .
value: "A deny gate can no longer be bypassed by a ccw-side failure (DEF-1)"
files: [src/claude_code_wrapper/hooks.py, src/claude_code_wrapper/router.py, src/claude_code_wrapper/manager.py, README.md, tests/test_pretooluse_fail_closed.py]
verify: "uv run pytest tests/test_pretooluse_fail_closed.py tests/test_router.py tests/test_manager.py -q && mise run lint"
depends_on: [hook-result-fields]
owns: []
uses: []
model: sonnet
estimate: 60
actual: ""
review: full
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# PreToolUse gate fails closed

## Requirement

DEF-1 (design D2): the PreToolUse gate fails closed, following the matching gate's `on_error`.

Today `SessionRouter.handle_claude_hook` catches every exception and returns allow (`router.py` handle_claude_hook), `SessionRouter._fire` swallows dispatcher exceptions into allow, `HookDispatcher.fire` allows on an unusable event, and redaction runs outside any per-action guard. Fix:

- `gate_fail_mode(hooks: HooksConfig, tool_name: str | None = None) -> Literal["allow", "deny"]` in hooks.py: `deny` when any blocking `onAgentToolCall` action with `on_error: deny` matches `tool_name` (a `None` matcher matches every tool; `tool_name=None` considers every action regardless of matcher). `SessionManager._gate_fail` delegates to it with `tool_name=None`, so `CCW_TOOL_GATE_FAIL` keeps its meaning (one formula).
- `HookDispatcher.fire` raises (instead of returning allow) when the event is unusable or redaction of the payload fails; `SessionRouter._fire` re-raises for `onAgentToolCall` and keeps swallowing for every other event.
- On any exception while handling `PreToolUse`, when `gate_fail_mode(cfg.hooks, tool_name)` is `deny`, the relay response is `deny_output("ccw hook relay failed: <ExceptionType>")`; an `error` event (kind `hook_relay`) and a `tool_denied` event are emitted, each best effort (a failing emit cannot turn the deny into an allow), and the tool-call metric counts a deny. When it is `allow` the behaviour is unchanged (allow plus the error report).
- No matching blocking gate with `on_error: deny`: nothing fails closed.
- README `### \`onAgentToolCall\``: state that a ccw-side failure denies the tool call when a matching blocking action has `on_error: deny` (the phrase "fails closed" appears).

## Acceptance conditions

- AC1: a dispatcher exception denies the tool when a matching blocking gate has on_error deny [evidence: test:tests/test_pretooluse_fail_closed.py::test_dispatcher_exception_denies_when_gate_denies]
- AC2: a redaction failure inside fire denies [evidence: test:tests/test_pretooluse_fail_closed.py::test_redaction_failure_denies]
- AC3: an event-log append failure in the PreToolUse handler denies [evidence: test:tests/test_pretooluse_fail_closed.py::test_emit_failure_denies]
- AC4: a tool not matched by any deny gate still allows on an exception [evidence: test:tests/test_pretooluse_fail_closed.py::test_unmatched_tool_still_allows]
- AC5: with no on_error deny gate an exception allows as today [evidence: test:tests/test_pretooluse_fail_closed.py::test_no_deny_gate_allows_on_exception]
- AC6: error and tool_denied events are emitted on a fail-closed deny [evidence: test:tests/test_pretooluse_fail_closed.py::test_error_and_tool_denied_events_emitted]
- AC7: gate_fail_mode honours matchers and the tool_name None case [evidence: test:tests/test_pretooluse_fail_closed.py::test_gate_fail_mode_matcher_aware]
- AC8: the manager gate env keeps its meaning through gate_fail_mode [evidence: command:uv run pytest tests/test_manager.py -k gate_env -q]
- AC9: README documents the fail-closed behaviour [evidence: command:grep -q "fails closed" README.md]

## Files

- `src/claude_code_wrapper/hooks.py`
- `src/claude_code_wrapper/router.py`
- `src/claude_code_wrapper/manager.py`
- `README.md`
- `tests/test_pretooluse_fail_closed.py`

## Interfaces

- `hooks.py::gate_fail_mode(hooks, tool_name=None)`; `manager.py::SessionManager._gate_fail` delegates to it.
- `router.py::SessionRouter.handle_claude_hook` PreToolUse failure path returns `hook_relay.deny_output(...)`.

## Expected outcome

With a blocking `onAgentToolCall` gate whose `on_error` is deny, no exception anywhere in ccw's PreToolUse handling lets the tool call through; without such a gate behaviour is unchanged.

## Steps

- Write `tests/test_pretooluse_fail_closed.py` forcing each failure path with monkeypatch (dispatcher.fire raising, redactor raising, event_log.append raising); see the deny tests fail.
- Implement `gate_fail_mode`, the re-raise in `_fire` for onAgentToolCall, the deny path in `handle_claude_hook`.
- README `onAgentToolCall` note.
- `uv run pytest tests/test_pretooluse_fail_closed.py tests/test_router.py tests/test_manager.py -q && mise run lint`.

## Notes

- Design D2: follow the matching gate's on_error, matcher-aware (`HookDispatcher._matches` is the matcher rule).
- The deny path must itself be exception-safe: guard each emit separately and return the deny regardless.
