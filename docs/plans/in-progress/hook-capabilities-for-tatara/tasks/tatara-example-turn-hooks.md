---
title: tatara example turn hooks
status: todo
repo: .
value: "A tested worked example of tatara's finaliser, issue capture, outcome gate and statusline as ccw hooks"
files: [src/claude_code_wrapper/data/examples/tatara.config.yaml, src/claude_code_wrapper/data/examples/tatara/tatara_hooks.py, examples/tatara.config.yaml, README.md, tests/test_example_tatara.py]
verify: "uv run pytest tests/test_example_tatara.py tests/test_examples.py -q && mise run lint"
depends_on: [service-exit-policy, missing-metric-families]
owns: []
uses: [hook-result-fields, on-stop-event, hook-context-env, fake-claude-scenarios, ccw-world-fixture]
model: sonnet
estimate: 90
actual: ""
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/hook-capabilities-for-tatara--tatara-example-turn-hooks
branch: hook-capabilities-for-tatara--tatara-example-turn-hooks
verified_head: ""
run: hook-capabilities-for-tatara
provides: []
acs_for_audit: []
process_gaps: []
---

# tatara example turn hooks

## Requirement

tatara example, part 1: the turn-path hooks (design D18; parity rows B1-B6). Shows how tatara's harness behaviour is expressed with ccw hooks; no tatara code paths in ccw core and no consumer repo changes.

Files and layout (same pattern as the workbench example):
- `src/claude_code_wrapper/data/examples/tatara.config.yaml` and `src/claude_code_wrapper/data/examples/tatara/tatara_hooks.py` (python stdlib only, one script with subcommands, executable header `#!/usr/bin/env python3`), plus the symlink `examples/tatara.config.yaml` -> `../src/claude_code_wrapper/data/examples/tatara.config.yaml` so `tests/test_examples.py` loads it.
- Every value from the environment uses `${VAR:-default}` interpolation so the config loads with the stub env of `tests/test_examples.py`; actions run `${TATARA_PYTHON:-python3} ${TATARA_HOOKS:-/opt/tatara/tatara_hooks.py} <subcommand>`. No secrets in any file; literal `$` in commands is written `$$`.

Subcommands and their config:
- `finalize` (onTurnComplete, NON-blocking, per R4: a blocking finaliser holds claude's Stop hook): reads the envelope on stdin; builds tatara's flat payload `{turnId, taskName (TATARA_TASK), state (completed->complete, failed/interrupted/timeout->failed), finalText, stopReason, error, durationSeconds, usage, pushedRepos, failedRepos, internalIssues, accountUsage}`; commits and pushes every git repo under the workdir that has changes (`git` subprocess; outcomes into pushedRepos/failedRepos); drains `$CCW_STATE_DIR/issues.jsonl`; reads `$CCW_CLAUDE_CONFIG_DIR/rate_limits.json` into accountUsage (absent when missing); signs the body with HMAC-SHA256 keyed by `CALLBACK_HMAC_SECRET` as `X-Tatara-Signature: sha256=<hex>`; POSTs to `DEFAULT_CALLBACK_URL`. While an outcome re-prompt is pending for the turn it posts nothing.
- `capture-issue` (blocking onAgentToolCall, matcher `mcp__tatara__report_internal_issue`): appends `{category, severity, summary}` (category and severity clamped to tatara's known sets, unknown -> `other`/`medium`) to `$CCW_STATE_DIR/issues.jsonl`; always allows.
- `outcome-result` (non-blocking onToolResult, matcher `mcp__tatara__submit_outcome`): records the result's `is_error` and the turn id in `$CCW_STATE_DIR/outcome.json`.
- `outcome-gate` (blocking onStop): when the turn's last submit_outcome was rejected and its re-prompt count is below `MAX_OUTCOME_REPROMPTS` (default 2; always below claude's cap of 8, per R2), prints `{"decision": "block", "reason": <re-prompt text>}` and increments the count in `$CCW_STATE_DIR`; otherwise allows.
- `statusline` (`claude.settings.statusLine` command, runs inside claude): reads claude's statusline JSON on stdin, writes its `rate_limits` to `$CLAUDE_CONFIG_DIR/rate_limits.json`, prints a short status line.

Test `tests/test_example_tatara.py` drives these against the fake claude through the `world` fixture with the example's hooks and settings (paths pointed at the repo script, `TATARA_PYTHON` = the test interpreter), using a loopback receiver for the callback and local bare git remotes.

README: a new `## Hook examples` section (after `## Hooks`) with `### tatara` describing the example, linking the files, and the mapping table parity row -> hook.

## Acceptance conditions

- AC1: the tatara example config loads with the stub env and its files ship inside the package [evidence: test:tests/test_example_tatara.py::test_tatara_config_loads_and_ships]
- AC2: the finaliser posts tatara's flat payload signed with X-Tatara-Signature [evidence: test:tests/test_example_tatara.py::test_finalizer_posts_signed_flat_payload]
- AC3: captured internal issues are drained into the callback [evidence: test:tests/test_example_tatara.py::test_issue_capture_drained_into_callback]
- AC4: a rejected outcome blocks the Stop once, the turn continues, and exactly one callback follows [evidence: test:tests/test_example_tatara.py::test_outcome_gate_blocks_once_then_allows]
- AC5: statusline rate limits appear as accountUsage [evidence: test:tests/test_example_tatara.py::test_statusline_rate_limits_in_account_usage]
- AC6: the finaliser commits and pushes changed repos and reports them [evidence: test:tests/test_example_tatara.py::test_finalizer_pushes_changed_repos]
- AC7: every example config still loads [evidence: command:uv run pytest tests/test_examples.py -q]
- AC8: README has the tatara example section [evidence: doc-section:README.md#tatara]

## Files

- `src/claude_code_wrapper/data/examples/tatara.config.yaml`
- `src/claude_code_wrapper/data/examples/tatara/tatara_hooks.py`
- `examples/tatara.config.yaml`
- `README.md`
- `tests/test_example_tatara.py`

## Interfaces

- `src/claude_code_wrapper/data/examples/tatara.config.yaml`, `src/claude_code_wrapper/data/examples/tatara/tatara_hooks.py` subcommands `finalize`, `capture-issue`, `outcome-result`, `outcome-gate`, `statusline`.

## Expected outcome

A reader can see, run and test how tatara's turn-complete callback, internal issues, outcome re-prompts and account usage are done with ccw hooks.

## Steps

- Write `tests/test_example_tatara.py`; see it fail.
- Write the config and script; add the symlink in `examples/`.
- README `## Hook examples` / `### tatara`.
- `uv run pytest tests/test_example_tatara.py tests/test_examples.py -q && mise run lint`.

## Notes

- Parity rows B1-B6 (design Research). Finaliser non-blocking (R4). Gate counts in CCW_STATE_DIR (R2).
- No tatara names in `src/claude_code_wrapper/*.py` (anti-goal).

- 2026-10-21 run hook-capabilities-for-tatara: dependency-failed on service-exit-policy, missing-metric-families
