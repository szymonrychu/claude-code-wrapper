---
title: Fake claude
status: todo
repo: claude-code-wrapper
value: Full lifecycle tests without real Claude
files: [tests/fake_claude/fake_claude.py, tests/conftest.py, tests/test_fake_claude.py, tests/fixtures/scenarios/basic.json]
verify: "uv run pytest tests/test_fake_claude.py -q && mise run lint"
depends_on: [repo-scaffold]
owns: [fake-claude]
uses: []
model: ""
estimate: 90
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Fake claude

## Requirement

Build the scripted fake `claude` used by every PTY, session and e2e test (D15). Stdlib-only Python at `tests/fake_claude/fake_claude.py`; module docstring documents the CLI, env vars and scenario schema below.

CLI: `fake_claude [--model M] [--effort E] [--session-id UUID] [-r|--resume ID] [--append-system-prompt T] [other flags accepted and recorded]`. When `FAKE_CLAUDE_LOG` is set, append one JSON line `{argv, cwd, env: {CLAUDE_CONFIG_DIR, CLAUDE_CODE_PROJECT_DIR_NAME, CCW_SESSION_ID, CCW_INTERNAL_URL}}` per invocation (no secret values). `--version` prints `0.0.0 (Fake Claude)`.

Plugin subcommands, state in `$CLAUDE_CONFIG_DIR/plugins/fake-state.json`: `plugin marketplace add <source> [--scope s] [--json]` (local dir sources must exist and contain `.claude-plugin/marketplace.json`; name read from it, else derived), `plugin marketplace list --json` (array of `{name, source, path}`), `plugin install <name@mkt> [-s scope] [-y] [--json]` (exit 1 when the marketplace is unknown or `FAKE_CLAUDE_PLUGIN_FAIL` equals the id), `plugin list --json` (array of `{id, version, scope, enabled}`).

Interactive mode (no subcommand): requires stdin to be a TTY (else exit 2). Boot: print a banner with ANSI color codes; when the scenario's `boot.bypass_dialog` (default true) print a dialog containing `Bypass Permissions mode` with `1. No, exit` and `2. Yes, I accept`, then wait for Down (`ESC [ B`) followed by CR; CR without a prior Down exits 1. Input that arrives before `boot.min_seconds` (default 0.3) exits 1. Then print the idle footer `? for shortcuts`.

Input: bracketed paste `ESC[200~ ... ESC[201~`; a CR that arrives in a later read than the paste end submits the prompt; a CR inside the same read chunk as the paste end is treated as a newline and does not submit. A lone ESC while a turn runs interrupts it.

Transcript: `$CLAUDE_CONFIG_DIR/projects/<CLAUDE_CODE_PROJECT_DIR_NAME or cwd with every non-alphanumeric char replaced by '-'>/<session id>.jsonl`. Session id from `--session-id`, from `--resume` (file must exist, appended to; missing prints `No conversation found with session ID` and exits 1), else uuid4. Lines use the real Claude Code shapes: `{"type":"user","message":{"role":"user","content":...},"uuid","sessionId","isSidechain":false,"cwd","timestamp"}` and `{"type":"assistant","message":{"id","model","role":"assistant","content":[{"type":"text"|"thinking"|"tool_use",...}],"usage":{"input_tokens","output_tokens","cache_read_input_tokens","cache_creation_input_tokens"}},...}`; tool results as user lines with `[{"type":"tool_result","tool_use_id","content","is_error"}]`.

Hooks: read `$CLAUDE_CONFIG_DIR/settings.json` `hooks.<Event>` = `[{matcher, hooks:[{type:"command", command, timeout}]}]`; run matching commands via `sh -c` with the JSON payload on stdin, env inherited plus `CLAUDE_PROJECT_DIR=<cwd>`, honouring timeout. Payloads carry `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `permission_mode` plus: UserPromptSubmit `prompt`; PreToolUse `tool_name`, `tool_input`, `tool_use_id`; PostToolUse/PostToolUseFailure also `tool_response`; SubagentStart `agent_id`, `agent_type`; SubagentStop `agent_id`, `agent_type`, `agent_transcript_path`, `stop_hook_active`; Stop `stop_hook_active: false`, `last_assistant_message`. PreToolUse deny = stdout JSON `hookSpecificOutput.permissionDecision == "deny"` or exit code 2 (reason from `permissionDecisionReason` or stderr); `additionalContext` from PreToolUse/PostToolUse is recorded into the transcript as a system line.

Scenario (`FAKE_CLAUDE_SCENARIO` = JSON file): `{"boot": {...}, "turns": [{"match": "<regex>", "steps": [...]}]}`; first turn whose regex matches the prompt runs, else default steps `[{"text": "echo: <prompt>"}]`. Steps: `{"text": s}`, `{"thinking": s}`, `{"tool": name, "input": {...}, "result": s, "fail": false}`, `{"subagent": {"id", "type", "text"}}` (writes `<session dir>/subagents/agent-<id>.jsonl` with `isSidechain: true` and runs SubagentStart/SubagentStop), `{"sleep": seconds}` (ESC during it writes the user line `[Request interrupted by user]`, prints it, returns to idle and runs NO Stop hook), `{"print": s}` (raw PTY output, used for fatal text, then idle without Stop), `{"exit": code}`, `{"background": {"after": seconds, "text": s}}` (after the turn ends, waits, appends an assistant line and runs Stop again). After the last step: run Stop hooks with the last assistant text, then print the footer.

`tests/conftest.py`: fixtures `fake_claude_bin(tmp_path)` (writes an executable shim `claude` that runs `sys.executable -I <path>/fake_claude.py "$@"`, returns its path), `scenario_file(tmp_path)` (factory writing a scenario dict to JSON) and `claude_config_dir(tmp_path)`. Fixture scenario `tests/fixtures/scenarios/basic.json` (text, thinking, one tool).

## Acceptance conditions

- AC1: Boot requires Down then CR on the bypass dialog and CR alone exits 1 [evidence: test:tests/test_fake_claude.py::test_boot_dialog_requires_down_then_cr]
- AC2: Paste followed by a separate CR submits the prompt [evidence: test:tests/test_fake_claude.py::test_paste_then_separate_cr_submits]
- AC3: A CR in the same chunk as the paste end does not submit [evidence: test:tests/test_fake_claude.py::test_same_chunk_cr_does_not_submit]
- AC4: The transcript is written at the pinned project dir and session id [evidence: test:tests/test_fake_claude.py::test_transcript_written_at_session_path]
- AC5: Stop hooks receive last_assistant_message and transcript_path [evidence: test:tests/test_fake_claude.py::test_stop_hook_receives_last_assistant_message]
- AC6: A PreToolUse deny blocks the tool and records an error tool_result [evidence: test:tests/test_fake_claude.py::test_pretooluse_deny_blocks_tool]
- AC7: ESC during a sleep step interrupts without running Stop hooks [evidence: test:tests/test_fake_claude.py::test_esc_interrupts_without_stop_hook]
- AC8: --resume appends to the same transcript file [evidence: test:tests/test_fake_claude.py::test_resume_appends_same_transcript]
- AC9: --resume with an unknown id exits 1 [evidence: test:tests/test_fake_claude.py::test_resume_unknown_id_exits_1]
- AC10: plugin marketplace add/list and plugin install/list keep state per config dir [evidence: test:tests/test_fake_claude.py::test_plugin_commands_state]
- AC11: Subagent steps run SubagentStart/Stop and write a sidechain transcript [evidence: test:tests/test_fake_claude.py::test_subagent_hooks_and_sidechain_transcript]
- AC12: A background step runs Stop again after the turn ended [evidence: test:tests/test_fake_claude.py::test_background_stop_after_turn]
- AC13: Early input before boot.min_seconds exits 1 [evidence: test:tests/test_fake_claude.py::test_early_input_exits]
- AC14: fake_claude.py imports only the standard library [evidence: test:tests/test_fake_claude.py::test_stdlib_only]

## Files

- `tests/fake_claude/fake_claude.py`
- `tests/conftest.py`
- `tests/test_fake_claude.py`
- `tests/fixtures/scenarios/basic.json`

## Interfaces

- Part `fake-claude`: `tests/fake_claude/fake_claude.py` (CLI, scenario schema in its docstring) and fixtures `fake_claude_bin`, `scenario_file`, `claude_config_dir` in `tests/conftest.py`. Consumed by pty-process, transcript-tailer, plugins-and-mise, session-boot, session-turns, session-manager, e2e-lifecycle.

## Expected outcome

A test can spawn `<fake_claude_bin>` under a PTY, answer the dialog, paste a prompt, and observe a real-shaped transcript and hook invocations driven by a settings.json, all in well under a second per turn.

## Steps

- Write `tests/test_fake_claude.py` (drives the fake with `pty.fork`/`os.openpty` directly, settings.json hooks that `cat >> <file>`); run red.
- Implement the fake incrementally (CLI and plugins, boot, input parsing, transcript, hooks, scenario steps), turning tests green one by one.
- `uv run pytest tests/test_fake_claude.py -q` then `mise run lint`.

## Notes

- Model after magic's stub `/Users/szymonri/Documents/magic-trainer/code/magic-cards-model-api/tests/testdata/stubclaude.py` and tatara's `W/cmd/wrapper/testdata/stubclaude/main.go` (read `git -C /Users/szymonri/Documents/tatara-new/code/tatara-claude-code-wrapper show origin/main:cmd/wrapper/testdata/stubclaude/main.go`).
- Real hook payload example: `git -C /Users/szymonri/Documents/tatara-new/code/tatara-claude-code-wrapper show origin/main:cmd/cc-stop-hook/testdata/hook_payload.json`.
- Real transcript lines: tatara `internal/transcript/testdata/*.jsonl` (same git show form) and magic `tests/wrapper/fixtures/`. Copy shapes, not secrets.
- You own `tests/conftest.py`; no other task edits it.
- Keep it deterministic and fast: default boot 0.3 s; no network.
