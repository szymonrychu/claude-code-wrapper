---
title: tatara example lifecycle
status: todo
repo: .
value: "A tested worked example of tatara's bootstrap, TTL gate, safety and metrics pushes and pod policy as ccw config"
files: [src/claude_code_wrapper/data/examples/tatara.config.yaml, src/claude_code_wrapper/data/examples/tatara/tatara_hooks.py, README.md, tests/test_example_tatara_lifecycle.py]
verify: "uv run pytest tests/test_example_tatara_lifecycle.py tests/test_example_tatara.py tests/test_examples.py -q && mise run lint"
depends_on: [tatara-example-turn-hooks]
owns: []
uses: [turn-metadata, on-tick-event, probe-alive-token, passthrough-globs, exit-policy, hook-context-env, ccw-world-fixture]
model: sonnet
estimate: 90
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# tatara example lifecycle

## Requirement

tatara example, part 2: lifecycle, bootstrap and pod policy (design D18; parity rows C1-C11, D1, D4-D6, E1, A9). Extends `tatara.config.yaml` and `tatara_hooks.py` from tatara-example-turn-hooks.

Subcommands and their config:
- `bootstrap` (`init.setup_commands`; runs with the allow-listed tool env, so the needed names are in `claude.passthrough_env`): clones `TATARA_REPOS` (JSON list of `{url, branch}`) or `REPO_URL`/`REPO_BRANCH` into `<workdir>/<owner>/<repo>` (depth 1 unless `TATARA_WORKSPACE_FULL_CLONE`), checks out `TASK_BRANCH` (created from the base branch when missing), writes `$CLAUDE_CONFIG_DIR/CLAUDE.md` directive blocks (headless, auto-push) and `<workdir>/.mcp.json` from `TATARA_EXTRA_MCP_SERVERS` (JSON). It is a skeleton of tatara's bootstrap, stated as such in its docstring.
- `ttl-gate` (blocking onTurn): the first turn records t0 in `$CCW_STATE_DIR`; once `AGENT_POD_TTL_SECONDS` have passed, ordinary turns are denied with reason `pod ttl exceeded`, and exactly one turn whose `data.metadata.handoff` is true is admitted.
- `safety-push` (onTick, `interval_seconds: ${BRANCH_PUSH_INTERVAL_SECONDS:-120}`, `when: busy`, non-blocking): `git push` of the task branch of every repo under the workdir; push only, never commit.
- `metrics-push` (onTick, `interval_seconds: 30`, `when: always`, non-blocking): GETs `$CCW_API_URL/metrics` and POSTs the text to `OPERATOR_PUSH_URL` with `RUN_ID` and `POD_NAME` as headers; a no-op when `OPERATOR_PUSH_URL` is unset.

Config: `sessions.exit_when_dead: true`, `sessions.probe_alive_token: TATARA-ALIVE`, `sessions.restart_continue_prompt` (a short "claude restarted, continue the task" prompt), `sessions.ask_user_question: deny`, `claude.passthrough_env` globs (`TATARA_*`, `CLI_OIDC_*`, `OIDC_ISSUER`, `RUN_ID`, `GIT_TOKEN`, `MISE_ALWAYS_KEEP_DOWNLOAD`, ...) with `claude.passthrough_env_exclude` for hook-only secrets (`CALLBACK_HMAC_SECRET`, `OPERATOR_PUSH_*`), model/effort/permission mode via `${MODEL:-}` style interpolation, and tatara's deny list in `claude.permissions.deny`.

Test `tests/test_example_tatara_lifecycle.py` against the fake claude via the `world` fixture and local bare git remotes. README `### tatara` gains a `#### tatara lifecycle` subsection (that exact heading) documenting the lifecycle hooks (`ttl-gate`, `safety-push`, `metrics-push`) and the bootstrap skeleton.

## Acceptance conditions

- AC1: past the TTL ordinary turns are vetoed and exactly one handoff turn is admitted [evidence: test:tests/test_example_tatara_lifecycle.py::test_ttl_gate_admits_one_handoff]
- AC2: the onTick safety push pushes the task branch during a running turn [evidence: test:tests/test_example_tatara_lifecycle.py::test_safety_push_on_tick]
- AC3: the onTick metrics push posts the scraped metrics to the receiver [evidence: test:tests/test_example_tatara_lifecycle.py::test_metrics_push_posts_scrape]
- AC4: bootstrap clones into the owner/repo layout and checks out the task branch [evidence: test:tests/test_example_tatara_lifecycle.py::test_bootstrap_clones_into_owner_repo_layout]
- AC5: the config sets exit_when_dead, the TATARA-ALIVE token, continuity and passthrough globs with excludes [evidence: test:tests/test_example_tatara_lifecycle.py::test_tatara_config_lifecycle_options]
- AC6: README tatara section documents the lifecycle hooks and the bootstrap skeleton [evidence: doc-section:README.md#tatara lifecycle]

## Files

- `src/claude_code_wrapper/data/examples/tatara.config.yaml`
- `src/claude_code_wrapper/data/examples/tatara/tatara_hooks.py`
- `README.md`
- `tests/test_example_tatara_lifecycle.py`

## Interfaces

- `tatara_hooks.py` subcommands `bootstrap`, `ttl-gate`, `safety-push`, `metrics-push`; config keys exit_when_dead, probe_alive_token, restart_continue_prompt, passthrough globs and excludes.

## Expected outcome

The tatara example covers pod bootstrap, the TTL/handoff admission, the safety and metrics pushes and the pod policy options, each tested.

## Steps

- Write `tests/test_example_tatara_lifecycle.py`; see it fail.
- Extend the config and script.
- Extend README `### tatara` with the `#### tatara lifecycle` subsection.
- `uv run pytest tests/test_example_tatara_lifecycle.py tests/test_example_tatara.py tests/test_examples.py -q && mise run lint`.

## Notes

- Parity rows C1-C11, D1, D4-D6, E1, A9. Bootstrap is a documented skeleton of tatara's Go bootstrap, not a port.
- R5: explicit globs plus excludes, never `*`.
