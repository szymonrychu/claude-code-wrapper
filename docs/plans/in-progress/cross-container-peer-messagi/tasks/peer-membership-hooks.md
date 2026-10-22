---
title: Peer membership hooks
status: in-progress
repo: .
value: "Operators can run their own logic before and after an instance joins or leaves the mesh, and can veto a join"
files: [src/claude_code_wrapper/config.py, src/claude_code_wrapper/hooks.py, src/claude_code_wrapper/data/config.example.yaml, README.md, tests/test_peer_hooks.py]
verify: "uv run pytest tests/test_peer_hooks.py tests/test_config.py tests/test_readme_api.py tests/test_hooks_command.py -q && mise run lint"
depends_on: [peer-events-and-metrics, peers-config-section]
owns: [peer-membership-hooks]
uses: [peers-config]
model: sonnet
estimate: 60
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Peer membership hooks

## Requirement

Instance-level membership hooks preJoin, postJoin, preLeave, postLeave (design D15).

- src/claude_code_wrapper/config.py: `HOOK_EVENTS` gains `preJoin`, `postJoin`, `preLeave`, `postLeave` (after `onError`), `HooksConfig` gains the four matching action lists (tests/test_config.py `test_hook_events_match_hooks_config_fields` keeps holding), and `VETO_EVENTS` gains `preJoin`. `onTick`-only keys (`interval_seconds`, `when`) stay rejected on them like on other events.
- src/claude_code_wrapper/hooks.py: `EVENT_DECISIONS` gives `preJoin` allow and deny, the other three allow only (preLeave annotations are still collected, its decision ignored). `INSTANCE_EVENTS = ("preJoin", "postJoin", "preLeave", "postLeave")`. `instance_dispatcher(cfg, redactor, *, service_env, api_url) -> HookDispatcher` builds a dispatcher with cwd `paths.sessions_root` (created if missing) and state dir `<sessions_root>/.peers-hook-state` (0700). `async fire_instance(dispatcher, event, data) -> HookOutcome` builds the usual payload with `session_id` `""`, `claude_session_id` None, `turn_id` None, `workdir` `""`, and never raises (a failure is logged and returns an allow outcome, or a deny for preJoin when the action's `on_error` is deny), so existing `blocking`, `timeout_seconds` and `on_error` semantics apply.
- src/claude_code_wrapper/data/config.example.yaml: the four hook keys under `hooks:` (empty lists with a comment each), so `test_full_example_loads` finds every HOOK_EVENTS name.
- README.md: under `## Hooks` one `### \`preJoin\`` (and postJoin, preLeave, postLeave) heading each: trigger and order (preJoin -> first heartbeat round -> postJoin before any session can be created; preLeave -> leaving heartbeat round -> postLeave before any onDestroy), payload `data` (`{instance, url, members: [{instance, url}]}`, post hooks add `failures: [{url, error}]`, postJoin adds `standalone`), decisions (preJoin deny = this instance runs standalone; the rest observe-only), and that they never fire with peers disabled (tests/test_readme_api.py `test_every_hook_event_documented`).
- tests/test_peer_hooks.py: the config accepts the four lists and rejects onTick keys on them; `EVENT_DECISIONS` for each; `fire_instance` with a command action receives the payload with an empty session id and the data, in the sessions-root cwd; a preJoin deny comes back as deny, a postJoin deny is treated as allow; a failing preJoin action with `on_error: deny` denies and with the default allows; a non-blocking postLeave returns at once.

Interfaces: `HOOK_EVENTS`/`VETO_EVENTS` additions (cross-epic `hook-events` and `hook-result-protocol` contracts owned by hook-capabilities-for-tatara), `hooks.INSTANCE_EVENTS`, `hooks.instance_dispatcher`, `hooks.fire_instance` (the `peer-membership-hooks` part).

## Acceptance conditions

- AC1: the four hook lists load from config and onTick-only keys are rejected on them [evidence: test:tests/test_peer_hooks.py::test_hook_config_lists]
- AC2: preJoin honours allow and deny and the other three honour only allow [evidence: test:tests/test_peer_hooks.py::test_event_decisions]
- AC3: fire_instance runs a command action with an empty session id, the membership data and the sessions-root cwd [evidence: test:tests/test_peer_hooks.py::test_fire_instance_payload]
- AC4: a preJoin deny and a failing preJoin with on_error deny both deny, and a postJoin deny is treated as allow [evidence: test:tests/test_peer_hooks.py::test_veto_and_on_error]
- AC5: every hook event is documented in the README and named in the example config [evidence: command:uv run pytest tests/test_readme_api.py tests/test_config.py tests/test_hooks_command.py tests/test_hook_result_fields.py -q]

## Files

- `src/claude_code_wrapper/config.py`
- `src/claude_code_wrapper/hooks.py`
- `src/claude_code_wrapper/data/config.example.yaml`
- `README.md`
- `tests/test_peer_hooks.py`


## Interfaces

See the Requirement's Interfaces paragraph; keep every name and signature stated there.

## Expected outcome

preJoin, postJoin, preLeave and postLeave are configurable hooks that run at instance level with the usual action semantics.

## Steps

- Write the tests named in the acceptance conditions in `tests/test_peer_hooks.py` first; run `uv run pytest tests/test_peer_hooks.py -q` and see them fail for the missing behaviour (not an import typo).
- Implement the smallest change that makes them pass; run `uv run pytest tests/test_peer_hooks.py -q` again and expect all passed.
- Run the task verify: `uv run pytest tests/test_peer_hooks.py tests/test_config.py tests/test_readme_api.py tests/test_hooks_command.py -q && mise run lint`; expect all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

Third README-editing task (README order: peers-config-section, peer-events-and-metrics, peer-membership-hooks, peer-messages-endpoint, peers-readme-guide.) config.example.yaml must name every HOOK_EVENTS entry.
