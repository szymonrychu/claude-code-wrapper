---
title: Workbench example
status: in-progress
repo: claude-code-wrapper
value: The workbench plugin flow runs inside the service out of the box
files: [examples/workbench.config.yaml, examples/docker-compose.workbench.yml, tests/test_examples.py]
verify: "uv run pytest tests/test_examples.py -q && mise run lint"
depends_on: [container]
owns: []
uses: [config-schema, container-image]
model: ""
estimate: 45
actual: ""
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/claude-code-wrapper-microser--workbench-example
branch: claude-code-wrapper-microser--workbench-example
verified_head: ""
run: claude-code-wrapper-microser
provides: []
acs_for_audit: []
process_gaps: []
---

# Workbench example

## Requirement

Ship the workbench example (D11): `examples/workbench.config.yaml`, `examples/docker-compose.workbench.yml`, `tests/test_examples.py`.

examples/workbench.config.yaml:
- `init.workdir`: `mode: mounted`, `path: /workdir/workbench` (fixed absolute path; comment explains task files store absolute worktree paths), with a commented clone alternative (`mode: clone`, `repo_url`, `name: workbench`, `submodules: true`).
- `init.git`: `user_name: ${GIT_USER_NAME}`, `user_email: ${GIT_USER_EMAIL}`.
- `claude.env`: `claude_code_oauth_token: ${CLAUDE_CODE_OAUTH_TOKEN}` (comment: the plugin's `claude -p` helpers strip ANTHROPIC_API_KEY, so OAuth is the working auth), `disable_autoupdater: true`, `extra: {WORKBENCH_ROOT: /workdir/workbench}`.
- `claude.passthrough_env`: `GH_TOKEN`, `GITLAB_TOKEN`, `SSH_AUTH_SOCK`, `WORKBENCH_SCM_HOSTS`.
- `claude.permission_mode: bypassPermissions`; `claude.config_files`: `{source: /opt/plugins/claude-infra-plugin/rules, target: rules}`; `prompts.global_claude_md: /etc/ccw/CLAUDE.md` commented as optional.
- `plugins`: marketplace `{name: szymonrychu-workbench, source: /opt/plugins/claude-infra-plugin}`, install `workbench@szymonrychu-workbench` (required), disable `superpowers@claude-plugins-official`.
- `mise`: `install_workdir: true`, `trusted_paths: [/workdir]`.
- `sessions`: `ask_user_question: forward`, `idle_timeout_seconds: 0` (long-lived sessions), `turn_stall_seconds: 3600`.
- `hooks`: an onAgentToolCall example that denies `git push --force` (command action, `matcher: Bash`, exit 2) and a commented onTurnComplete webhook.
examples/docker-compose.workbench.yml (override for docker-compose.yml): mounts `${WORKBENCH_PLUGIN_DIR:-~/Documents/claude-infra-plugin}:/opt/plugins/claude-infra-plugin:ro`, `${WORKBENCH_TREE:?set WORKBENCH_TREE}:/workdir/workbench`, `${SSH_DIR:-~/.ssh}:/home/agent/.ssh:ro`, `./examples/workbench.config.yaml:/etc/ccw/config.yaml:ro`, and passes `GIT_USER_NAME`, `GIT_USER_EMAIL`, `WORKBENCH_SCM_HOSTS`.
tests/test_examples.py: every `config.example.yaml` and `examples/*.yaml` config loads through `load_config` with stub env values; the workbench config has the plugin id, an absolute directory marketplace source, the fixed absolute mounted workdir, `ask_user_question: forward`, superpowers disabled and the rules copy.

## Acceptance conditions

- AC1: Every example config loads with stub env values [evidence: test:tests/test_examples.py::test_every_example_config_loads]
- AC2: The workbench config installs the workbench plugin from the mounted directory marketplace and disables superpowers [evidence: test:tests/test_examples.py::test_workbench_example_plugin]
- AC3: The workbench config uses a fixed absolute mounted workdir and forwards AskUserQuestion [evidence: test:tests/test_examples.py::test_workbench_example_workdir_and_questions]
- AC4: The workbench config copies the plugin rules into the config dir [evidence: test:tests/test_examples.py::test_workbench_example_rules_copied]
- AC5: The compose override is valid together with the base file [evidence: command:WORKBENCH_TREE=/tmp CCW_PORT=18081 docker compose -f docker-compose.yml -f examples/docker-compose.workbench.yml config -q]

## Files

- `examples/workbench.config.yaml`
- `examples/docker-compose.workbench.yml`
- `tests/test_examples.py`

## Interfaces

- `examples/workbench.config.yaml` (a `config-schema` instance) and `examples/docker-compose.workbench.yml` (an override of the `container-image` compose file).

## Expected outcome

`WORKBENCH_TREE=<tree> docker compose -f docker-compose.yml -f examples/docker-compose.workbench.yml up` gives sessions that start at the tree root with the workbench plugin, rules and tools in place.

## Steps

- Write `tests/test_examples.py`; red; write the two example files; green; run the compose config AC; `mise run lint`.

## Notes

- Plugin facts: marketplace name `szymonrychu-workbench`, plugin `workbench`, source `/Users/szymonri/Documents/claude-infra-plugin` on the host (no git remote, so it is mounted read-only); its hooks need bash, python3 >= 3.11, git, jq, node, mise, uv (all in the image).
- Never enable two plugins named `workbench`; disable superpowers.

- 2026-10-20 run claude-code-wrapper-microser: dependency-failed on container
