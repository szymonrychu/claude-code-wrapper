---
title: Settings render
status: todo
repo: claude-code-wrapper
value: Every Claude lifecycle point is wired to the service with config permissions
files: [src/claude_code_wrapper/bootstrap/settings.py, tests/test_bootstrap_settings.py]
verify: "uv run pytest tests/test_bootstrap_settings.py -q && mise run lint"
depends_on: [home-seed, hook-relay]
owns: [settings-renderer]
uses: [config-schema, hook-relay-protocol]
model: ""
estimate: 60
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Settings render

## Requirement

Render `<config_dir>/settings.json` for a session (D7, D11, D13) in `src/claude_code_wrapper/bootstrap/settings.py`.

`render_settings(config_dir, cfg: Config, relay_command: str | None = None) -> dict` (returns the written document):
- Loads and merges an existing settings.json (claude and `claude plugin install` write `enabledPlugins` and `extraKnownMarketplaces` there; those keys survive).
- Relay hooks: for every event in `CLAUDE_HOOK_EVENTS` from `hook_relay.py` (UserPromptSubmit, PreToolUse, PostToolUse, PostToolUseFailure, Stop, SubagentStart, SubagentStop) a matcher group `{"matcher": "", "hooks": [{"type": "command", "command": "<relay> <Event>", "timeout": T}]}`. Previously rendered relay entries (command starting with the relay path) are replaced, never duplicated; any other hook entries are kept. Timeouts: PreToolUse = max(timeout_seconds of blocking onAgentToolCall actions, default 10) + 5; PostToolUse and PostToolUseFailure = max(blocking onToolResult timeouts, 10) + 5; Stop 30; others 15.
- `relay_command` default: the absolute path of `ccw-hook` next to `sys.executable` when that file exists, else `ccw-hook`.
- `permissions.defaultMode = claude.permission_mode`; `permissions.allow = claude.permissions.allow`; `permissions.deny = claude.permissions.deny` plus `AskUserQuestion` when `sessions.ask_user_question == "deny"` (no duplicates).
- `enableAllProjectMcpServers: true` unless the overlay sets it.
- `enabledPlugins[<id>] = false` for every `plugins.disable` id.
- `claude.settings` overlay deep-merged last, except `hooks`: overlay matcher groups are appended per event, so an overlay can add hooks but cannot remove the relay hooks.
- Atomic write, mode 0600. Rendering twice with the same inputs produces identical files.

## Acceptance conditions

- AC1: Every Claude hook event gets one relay command entry [evidence: test:tests/test_bootstrap_settings.py::test_relay_hooks_for_every_event]
- AC2: PreToolUse and PostToolUse timeouts follow the blocking hook timeouts [evidence: test:tests/test_bootstrap_settings.py::test_gate_timeouts_from_blocking_hooks]
- AC3: Old relay entries are replaced and foreign hook entries kept [evidence: test:tests/test_bootstrap_settings.py::test_existing_relay_entries_replaced_others_kept]
- AC4: Permissions mode, allow and deny render from config [evidence: test:tests/test_bootstrap_settings.py::test_permissions_rendered]
- AC5: ask_user_question deny adds AskUserQuestion to the deny list once [evidence: test:tests/test_bootstrap_settings.py::test_ask_user_question_deny_mode]
- AC6: Disabled plugins are written as false in enabledPlugins [evidence: test:tests/test_bootstrap_settings.py::test_disabled_plugins_false]
- AC7: Overlay hooks are appended and cannot remove relay hooks [evidence: test:tests/test_bootstrap_settings.py::test_overlay_merged_hooks_appended]
- AC8: Re-rendering is idempotent [evidence: test:tests/test_bootstrap_settings.py::test_idempotent_rerender]
- AC9: enabledPlugins written by the plugin CLI is preserved [evidence: test:tests/test_bootstrap_settings.py::test_enabled_plugins_written_by_cli_preserved]
- AC10: The relay command resolves next to sys.executable when present [evidence: test:tests/test_bootstrap_settings.py::test_relay_command_resolution]

## Files

- `src/claude_code_wrapper/bootstrap/settings.py`
- `tests/test_bootstrap_settings.py`

## Interfaces

- `claude_code_wrapper.bootstrap.settings.render_settings`. Part `settings-renderer`; consumed by session-manager. Uses `CLAUDE_HOOK_EVENTS` from `hook_relay.py` (part `hook-relay-protocol`).

## Expected outcome

A session's claude calls `ccw-hook <Event>` for every lifecycle point the service needs, with permission mode and deny list from config, while plugin enablement written by the CLI and user overlay hooks stay intact.

## Steps

- Write `tests/test_bootstrap_settings.py`; red; implement settings.py; green; `mise run lint`.

## Notes

- Sources: tatara `W/internal/bootstrap/settings.go:10-73`, magic `M/bootstrap.py:75-93`.
- Do not copy any workbench plugin hook into settings (double-fire, design anti-goal); the plugin registers its own.
- `bootstrap/__init__.py` belongs to home-seed; re-export `render_settings` from there only if home-seed already does it, otherwise import from the module path.
