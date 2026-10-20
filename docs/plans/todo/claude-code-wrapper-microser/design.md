# claude-code wrapper microservice design

## Summary

`claude-code-wrapper` (Python package `claude_code_wrapper`, CLI `ccw`) is an HTTP service that runs
N long-lived interactive `claude` sessions, each under a PTY (user decision D1), each with its own
workdir under the `workdir` volume and its own `CLAUDE_CONFIG_DIR` under the `sessions` volume.
Turns are pasted into the TUI; completion arrives from Claude's Stop hook through a relay to a
loopback endpoint; tool calls, subagents and prompts arrive the same way; assistant text comes from
tailing the JSONL transcript. Every event lands in a per-session event log, streamed over SSE and
pageable, and fans out to user hooks (command or webhook actions) declared in config.yaml. The
config models every known Claude Code env var as a typed field (user decision D2). It ships with a
Dockerfile, docker-compose.yml, an example config and a workbench example config, a README covering
every endpoint, event, hook and config key, and docs/source-analysis.md.

## Intent and why

The user has three projects that each wrap Claude Code: tatara (Go, PTY, operator-driven pods),
magic-trainer (a Python port of the tatara wrapper, one match per container) and ros2 (Agent SDK
service for a robot). The request: extract the methodology into one separate, configurable
microservice, compatible with the workbench project (the `workbench` plugin flow), implemented in
Python test-first, documented, and runnable with docker-compose with volumes for workdirs and past
sessions. Configurable lifecycle hooks are the extension point that replaces each project's
hardcoded domain glue.

## Goals

- G1 `docker compose up` (repo root) builds and starts the service as a non-root user, with named
  volumes `workdir` (mounted at `/workdir`), `sessions` (mounted at `/sessions`) and `mise`
  (`/opt/mise`), and `config.example.yaml` (or `$CCW_CONFIG`) mounted at `/etc/ccw/config.yaml`.
- G2 Session lifecycle over HTTP: create, submit turn, poll turn, SSE events, interrupt, probe,
  transcript, destroy, list past sessions, resume by id after a service restart.
- G3 config.yaml with every section in "Config schema" below, `${VAR}` interpolation, strict
  validation with dotted-path errors.
- G4 `claude.env`: typed fields for every known Claude Code env var, `extra` passthrough, global
  default plus per-session override at create and resume, secrets redacted everywhere.
- G5 Hooks: every event in "Hook events" fires its configured actions; blocking onAgentToolCall can
  deny a tool, blocking onTurn can veto a turn, blocking onCreate/onResume can abort.
- G6 Workbench compatibility per D11.
- G7 Test suite green via `mise run test`, lint via `mise run lint`; e2e lifecycle test with a fake
  `claude`.
- G8 README.md and docs/source-analysis.md complete, with mechanical coverage tests.

## Non-goals

- Agent SDK or `claude -p` backends (D1), including magic-trainer's one-shot `/v1/review` route.
- WebSocket streaming; Kubernetes manifests; metrics push; OIDC (bearer token only).
- Domain features of the sources: tatara commit/push and outcome re-prompt, game seat driver,
  robot gate and plan tools, context digests, image thumbnails. Users build these with hooks.
- Remote, CI, image publishing, deploy.

## Anti-goals

- A wrapper that parses terminal output for results (results come from hooks and transcripts only;
  the PTY ring is for dialogs and fatal patterns).
- Writing service files into the user's workdir (CLAUDE.md, .mcp.json, settings): a workbench tree
  must stay clean. All service-rendered files live in the session `CLAUDE_CONFIG_DIR`.
- A secret value (OAuth token, API key, hook token, webhook secret, server token) appearing in any
  API response, SSE frame, hook payload, log line or session.json.
- Double-firing the workbench plugin's own hooks by copying them into settings.
- A turn slot that never frees (interrupt with no Stop hook, fatal auth error at the prompt).

## Constraints

- Python 3.12, uv, hatchling src layout, FastAPI + uvicorn, pydantic v2, PyYAML, httpx,
  prometheus-client; dev: pytest, pytest-asyncio (asyncio_mode auto), ruff (E,F,I,UP,B,SIM, line
  100). All runtime and dev deps are declared in the scaffold task so no later task edits
  pyproject.toml or uv.lock. (my call)
- `mise.toml` tasks `install`, `test`, `lint`, `fmt`, `run`, `compose`, `compose:down`,
  `docker:build`; `test` and `lint` names are load-bearing for the workbench facts pack.
- `.claude/workbench.json`: `{"suites": ["mise run lint", "mise run test"]}`.
- Plain ASCII in every file (no em dashes, smart quotes, arrows).
- `claude` refuses bypassPermissions as root: the container runs as uid 1000 `agent`.
- No `--dangerously-skip-permissions` (adds a dialog, tatara spike); permission mode is set in
  settings.json `permissions.defaultMode`.
- Only `tests/conftest.py`'s owner task (fake-claude) edits it; other tasks keep fixtures in their
  own test modules.
- Probed facts (Claude Code 2.1.304 on this host, 2026-10-20): `--session-id <uuid>`,
  `-r/--resume <id>`, `--fork-session`, `--effort`, `--model`, `--append-system-prompt`,
  `--settings` exist; `claude plugin marketplace add <source> [--scope] [--json]`,
  `claude plugin install <id> [-s scope] [-y] [--json]`, `claude plugin list --json` (array of
  `{id, version, scope, enabled, installPath, ...}`), `claude plugin marketplace list --json` (array
  of `{name, source, repo|path, installLocation}`); the binary knows hook events SubagentStart,
  PostToolUseFailure, UserPromptSubmit and env vars CLAUDE_CODE_PROJECT_DIR_NAME,
  CLAUDE_CODE_PLUGIN_SEED_DIR and the `.claude.json` key `bypassPermissionsModeAccepted`.

## Architecture

```
client --HTTP/SSE--> public API (server.host:port, bearer auth)
                        |
                   SessionManager --- per session: SessionRouter
                        |                 |-- Session (PtyProcess + boot/turn state machine)
                        |                 |-- TranscriptTailer + SubagentWatcher
                        |                 |-- EventLog (sessions/<id>/events.jsonl, ring, subscribers)
                        |                 '-- HookDispatcher (command / webhook actions)
claude (PTY child) --settings.json hooks--> ccw-hook <Event> --POST--> internal API 127.0.0.1:8090
                                                                       '-> SessionRouter.handle_claude_hook
```

Module layout (`src/claude_code_wrapper/`): `claude_env.py`, `redact.py`, `config.py`,
`interpolation.py`, `ring.py`, `pty_proc.py`, `bootstrap/{__init__,claude_json,instructions,settings}.py`,
`hook_relay.py`, `hooks.py`, `events.py`, `transcript.py`, `workdir.py`, `plugins.py`, `tooling.py`,
`session.py`, `turns.py`, `router.py`, `prompts.py`, `manager.py`, `internal_api.py`,
`api/{__init__,app,auth,errors,sessions,turns,events}.py`, `metrics.py`, `service.py`, `cli.py`,
`__main__.py`. Tests under `tests/`, the fake claude at `tests/fake_claude/fake_claude.py`.

Session directory (`<paths.sessions_root>/<session_id>/`): `session.json` (metadata, never secret
values), `config/` (the session `CLAUDE_CONFIG_DIR`: `.claude.json`, `settings.json`, `CLAUDE.md`,
copied config files such as `rules/`, `projects/workdir/<claude_session_id>.jsonl`, `plugins/`),
`events.jsonl`, `turns.jsonl`, `gitconfig`, `setup.done`. Workdir: `<paths.workdir_root>/<name>`
(modes empty and clone) or the configured absolute path (mode mounted).

Ids: `session_id` = `ses_` + 12 hex chars; `claude_session_id` = uuid4 passed as `--session-id` on
first spawn and `--resume` afterwards; `turn_id` = `turn-` + base36 nanoseconds, strictly monotonic
even when the clock repeats; hook token = `secrets.token_urlsafe(32)` per session (in memory and in
the claude child env only). (my call)

## Config schema

Top-level keys (pydantic, `extra="forbid"` everywhere; `${VAR}`, `${VAR:-default}` and `$$`
interpolation on string scalars before validation). Defaults shown.

```yaml
server:   {host: 0.0.0.0, port: 8080, internal_port: 8090, auth_token: "", log_level: info,
           callback_secret: ""}            # internal host is always 127.0.0.1
paths:    {workdir_root: /workdir, sessions_root: /sessions}
claude:
  binary: claude
  model: ""                # --model
  effort: ""               # --effort
  permission_mode: bypassPermissions   # settings.json permissions.defaultMode
  extra_args: []           # appended to argv; --dangerously-skip-permissions rejected
  env: {}                  # ClaudeEnv (D2): typed fields + extra
  passthrough_env: []      # names copied from the service env when present (GH_TOKEN, SSH_AUTH_SOCK, ...)
  permissions: {allow: [], deny: [EnterPlanMode, ExitPlanMode]}
  settings: {}             # deep-merged overlay into settings.json (hooks lists are appended)
  claude_json: {}          # deep-merged overlay into .claude.json
  mcp_servers: {}          # name -> {type: stdio|http|sse, command, args, env, url, headers}
  config_files: []         # [{source, target}] copied into CLAUDE_CONFIG_DIR (target relative)
  boot: {timeout_seconds: 60, min_boot_seconds: 4.0, idle_ready_seconds: 1.5,
         submit_delay_seconds: 0.4, dialogs: [<defaults>], ready_markers: [<defaults>],
         fatal_patterns: [<defaults>]}
init:
  workdir: {mode: empty, path: "", name: "", repo_url: "", branch: "", depth: 0, submodules: false}
  git: {user_name: "", user_email: ""}
  setup_commands: []       # [{run, timeout_seconds: 600}]
prompts:
  global_claude_md: ""     # file whose content starts <config>/CLAUDE.md
  system_append: ""        # appended to <config>/CLAUDE.md
  initial_prompt: ""       # string, or {template, variables}
  templates: {}            # name -> {text, description, variables: {name: {required, default}}}
mise:     {tools: {}, config_file: "", install_on_start: true, install_workdir: true,
           trusted_paths: [/workdir], required: true, install_timeout_seconds: 900,
           global_config_path: /home/agent/.config/mise/config.toml}
plugins:  {marketplaces: [], install: [], disable: [], timeout_seconds: 180}
          # marketplaces: [{name, source}]; install: [{plugin: name@marketplace, scope: user, required: true}]
hooks:    {onCreate: [], onResume: [], ...}   # event -> [action]
sessions: {max_sessions: 4, idle_timeout_seconds: 0, turn_stall_seconds: 1800,
           interrupt_settle_seconds: 20, max_restarts: 3, ask_user_question: forward,
           turn_marker: true, resume_on_start: false}
limits:   {max_prompt_bytes: 1048576, max_file_bytes: 262144, max_files_per_turn: 16,
           turn_max_seconds: 0, event_ring_size: 1000, event_log_max_bytes: 52428800,
           sse_replay: 100, sse_heartbeat_seconds: 15, shutdown_timeout_seconds: 10}
```

Hook action: `{type: command, run, timeout_seconds: 30, blocking: false, on_error: allow,
matcher: null, env: {}}` or `{type: webhook, url, secret: "", headers: {}, timeout_seconds: 10,
retries: 3, blocking: false, on_error: allow, matcher: null}`. `matcher` (regex, full match on the
tool name) is valid only on onAgentToolCall and onToolResult.

Default boot dialogs: `{pattern: "Bypass Permissions mode", keys: ["\u001b[B", "\r"], delay_seconds:
0.6}` and `{pattern: "Do you trust the files in this folder", keys: ["\r"]}`. Default ready markers:
`"? for shortcuts"`, `"bypass permissions on"`. Default fatal patterns: `"API Error: 401"`,
`"API Error: 403"`, `"Please run /login"`, `"Credit balance is too low"`, `"OAuth token has expired"`.

## Hook events

| Event | Fired from | Blocking effect | `data` |
|---|---|---|---|
| onCreate | manager, after claude is ready | deny aborts creation (session stopped, 403) | session snapshot |
| onResume | manager, after resumed claude is ready | deny aborts resume | session snapshot |
| onDestroy | manager, after claude stopped | awaited, cannot veto | session snapshot |
| onTurn | router, before paste | deny vetoes the turn (403 `vetoed`) | `{text, files, origin}` |
| onTurnComplete | Stop relay | awaited, cannot veto | turn record |
| onAgentResponse | transcript assistant text (main thread) | awaited, cannot veto | `{text, message_id, model}` |
| onAgentToolCall | PreToolUse relay | deny blocks the tool call | `{tool_name, tool_input, tool_use_id}` |
| onToolResult | PostToolUse / PostToolUseFailure relay | additional_context returned to Claude | `{tool_name, tool_input, tool_response, tool_use_id, is_error}` |
| onSubagentStart | SubagentStart relay | awaited | `{agent_id, agent_type}` |
| onSubagentStop | SubagentStop relay | awaited | `{agent_id, agent_type, agent_transcript_path}` |
| onInterrupt | router, on interrupt request | awaited | `{turn_id}` |
| onError | router/manager on fatal pattern, crash, boot timeout, step failure, hook relay error | awaited | `{kind, message, step?}` |

Payload envelope (command stdin, webhook body), redacted: `{"event", "session_id",
"claude_session_id", "turn_id", "ts", "workdir", "data"}`. Command env: `CCW_EVENT`,
`CCW_SESSION_ID`, `CCW_TURN_ID`, `CCW_WORKDIR`, `CCW_CLAUDE_CONFIG_DIR` plus action `env`. Command
result: exit 0 allow (optional stdout JSON `{decision, reason, additional_context}`), exit 2 deny with
stderr as reason, other exit or timeout = error handled by `on_error`. Webhook: POST JSON with
`X-CCW-Event`, `X-CCW-Delivery`, and `X-CCW-Signature: sha256=<hex HMAC-SHA256(secret, body)>` when
`secret` is set; retries with 0.5 s doubling backoff; a blocking webhook's 2xx JSON body uses the
same decision shape. Deny wins across actions.

## Event types

Envelope `{seq, ts, session_id, type, data}`. Types: `session_state`, `turn_started`,
`turn_rejected`, `turn_completed`, `turn_failed`, `assistant_text`, `thinking`, `tool_call`,
`tool_result`, `tool_denied`, `question`, `subagent_started`, `subagent_stopped`, `interrupted`,
`stall_suspected`, `probe`, `hook_result`, `error`, `session_closed`.

## Public API

All under bearer auth except `/healthz`, `/readyz`, `/metrics`. Error body
`{"error": {"code", "message"}}`.

| Method | Path | Result |
|---|---|---|
| POST | /v1/sessions | 201 snapshot; 422 invalid body or env override; 429 max_sessions; 403 vetoed; 500 create_failed (step) |
| GET | /v1/sessions | active and persisted sessions |
| GET | /v1/sessions/{id} | snapshot (env redacted) |
| DELETE | /v1/sessions/{id}?purge=false | 200 stopped snapshot; purge removes session dir and non-mounted workdir |
| POST | /v1/sessions/{id}/resume | 200 snapshot; 409 not resumable |
| POST | /v1/sessions/{id}/turns | 202 `{turn_id, state}`; 400, 413, 409 busy, 503 booting/dead, 403 vetoed |
| GET | /v1/sessions/{id}/turns | turn summaries |
| GET | /v1/sessions/{id}/turns/{turn_id} | full turn record |
| POST | /v1/sessions/{id}/interrupt | `{interrupted, turn_id}` |
| POST | /v1/sessions/{id}/probe | 202 `{probe_id, state}` |
| GET | /v1/sessions/{id}/probe/{probe_id} | probe status |
| GET | /v1/sessions/{id}/transcript | application/x-ndjson, redacted |
| GET | /v1/sessions/{id}/history?after_seq&limit | `{events, next_seq, has_more}` |
| GET | /v1/sessions/{id}/events | text/event-stream (Last-Event-ID or after_seq replay, heartbeats) |
| GET | /v1/config | effective config, redacted |
| GET | /v1/prompts | templates |
| GET | /healthz, /readyz, /metrics | ops |

Internal (127.0.0.1 only): `POST /internal/hooks/{ClaudeEvent}` with `X-CCW-Session` and
`X-CCW-Hook-Token`; response `{"stdout", "stderr", "exit_code"}`; `GET /internal/healthz`.

Turn record: `turn_id, session_id, origin (client|initial|agent), state (running|completed|failed|
interrupted|timeout), text, final_text, stop_reason, usage, model_tokens, error, callback_url,
started_at, completed_at, last_activity_at, stall_suspected_since, tool_calls`.

## Source findings

The briefs were temporary; these are the findings the plan relies on. Path prefixes:
`W/` = /Users/szymonri/Documents/tatara-new/code/tatara-claude-code-wrapper (read `origin/main`,
the checkout is on a side branch), `M/` = /Users/szymonri/Documents/magic-trainer/code/magic-cards-model-api/src/mtg_agent/wrapper,
`R/` = /Users/szymonri/Documents/ros2-lerobot-sverve-platform/nodes/claude_agent/claude_agent.

tatara (Go): one pod = one interactive claude under creack/pty (`W/internal/session/pty.go:51-77`,
winsize 50x200, argv only `--model`, `--effort`, `--continue`); `~/.claude.json` seed with onboarding,
trust for the RESOLVED cwd, API-key suffix approval (`W/internal/bootstrap/claudejson.go:16-69`);
bypass warning answered with Down then CR, readiness by quiescence >= 4 s and idle >= 1.5 s, 64 KiB
ANSI-stripped ring, ring reset between relaunches (`W/internal/session/session.go:641-707`, 825);
submit = bracketed paste, about 400 ms, separate CR, 409 while busy (`session.go:1170-1235`); Stop
hook binary posts to `127.0.0.1:8090/internal/turn-complete`, 5 x 5 s retries, always exit 0, uses
`last_assistant_message` (`W/cmd/cc-stop-hook/`); stale hooks rejected by state and session id
(`session.go:1538-1646`); transcript tailer, 200 ms poll, inode reopen, 16 MiB partial cap, typed
blocks, subagent `subagents/agent-*.jsonl` watcher (`W/internal/transcript/tailer.go:530`,
`session.go:1404-1495`); redaction longest-first (`W/internal/transcript/redact.go`); interrupt =
one ESC, resolved by `[Request interrupted by user` (`W/internal/session/interrupt.go:55`); probe
mid-turn; lifecycle hooks as shell commands best-effort (`W/internal/bootstrap/hooks_lifecycle.go:22-59`);
webhook with HMAC `sha256=<hex>` and retries (`W/internal/webhook/webhook.go:164`); settings deny
list incl. AskUserQuestion/ExitPlanMode/EnterPlanMode (`W/internal/bootstrap/settings.go:10-73`);
MCP merge into `.mcp.json` (`W/internal/bootstrap/mcp.go`); image non-root uid 10001, pinned
claude npm layer, mise with shims PATH (`W/Dockerfile`); crash relaunch with `--continue` guarded
by transcript existence (`session.go:796-907`); cross-pod resume by copying the JSONL and
`--resume <sid>` was spiked and works (`W/docs/conversation-resume-spike.md`).

magic-trainer (Python port of tatara): two uvicorn servers, public `:8080` and loopback
`127.0.0.1:8090` (`M/api.py:1-7, 317-325`); `pty.fork`, close order SIGTERM group then close fd then
SIGKILL after 5 s (`M/pty_proc.py:73-95`, macOS deadlock otherwise); fatal needles 401/403/login/credit
/expired fail the turn because claude sits at the prompt (`M/session.py:45-68, 258-290`); turn marker
`<!-- wrapper turn: ... -->` in the prompt because Stop carries no turn id (`M/transcript.py:30`);
no Stop hook on interrupted turns, second ESC after 20 s and fail the turn (`M/session.py:728-797`);
`files` written into the workspace before submit with path-escape and 256 KiB guard
(`M/api.py:46-80`); OAuth token wins, API key popped (`M/pty_proc.py:133-143`); `MAX_THINKING_TOKENS`
set or popped; usage dedupe by message id (`M/transcript.py:124-148`); stub interactive claude for
tests (`/Users/szymonri/Documents/magic-trainer/code/magic-cards-model-api/tests/testdata/stubclaude.py`)
and injectable spawn/now/new_id seams (`M/session.py:128-150`); claude refuses bypass as root
(`Dockerfile.agent:31-47`); MCP description/output limits via env (`docker-compose.yml:190-198`).

ros2 (Agent SDK, not adopted as backend per D1): `POST /api/message` 202 then events, 409 busy
(`R/api.py:141-156`); event log JSONL with seq continuing across restarts, RAM ring, paging,
rotation by dropping the oldest half, subscriber queues (`R/events.py:54-226`); WS history on
connect then live with seq de-dup (`R/api.py:170-189`); exception-safe tool gate that turns errors
into explicit denies (`R/tools.py:319-340`); PostToolUse `additionalContext` (`R/runner.py:514-526`);
child env built explicitly, API keys stripped, `DISABLE_AUTOUPDATER=1`,
`ENABLE_CLAUDEAI_MCP_SERVERS=false` with OAuth (`R/runner.py:234-255`); bounded shutdown with child
kill and uvicorn `timeout_graceful_shutdown=3` (`R/runner.py:1915-1959`, `R/__main__.py:25`);
pydantic config, `extra="forbid"`, exit 78 on invalid (`R/__main__.py:62-66`).

workbench (compatibility): the plugin `workbench@szymonrychu-workbench` comes from a directory
marketplace at `/Users/szymonri/Documents/claude-infra-plugin` (no git remote: it must be mounted);
claude must start at the tree root (plans root = git top of cwd); task files store absolute
worktree paths, so the workdir path must be fixed; plugin hooks need bash, python3 >= 3.11, git, jq,
node, mise, uv, ruff, shellcheck, gh/glab, ssh; flows rely on `~/.claude/CLAUDE.md` and
`~/.claude/rules/*.md` (copied via `claude.config_files`); the plugin's `claude -p` helpers strip
`ANTHROPIC_API_KEY`, so `CLAUDE_CODE_OAUTH_TOKEN` is the working auth; long-lived sessions are
required (Workflow, Monitor, background Bash outlive a turn).

Docs (code.claude.com, 2026-10): transcripts at `$CLAUDE_CONFIG_DIR/projects/<project>/<id>.jsonl`;
`CLAUDE_CODE_PROJECT_DIR_NAME` pins `<project>` when `CLAUDE_CONFIG_DIR` is set; persisting
`CLAUDE_CONFIG_DIR` keeps `.claude.json` with it; resume by id; plugins via `claude plugin
marketplace add` / `install`, `CLAUDE_CODE_SYNC_PLUGIN_INSTALL`; mise `install --locked`,
`MISE_TRUSTED_CONFIG_PATHS`, data dir outside a mounted HOME; Anthropic policy note: products for
third parties should use API keys unless approved (README states it; the service passes both).

## Decisions

### D1: Drive interactive claude under a PTY
User decision. The backend spawns interactive `claude` under a PTY (tatara/magic method), never the
Agent SDK and never `-p`. Alternatives: Agent SDK `ClaudeSDKClient` (ros2), `-p --output-format
stream-json`. Why: same harness a human gets (system prompt, skills, plugin hooks, Workflow,
Monitor), long-lived sessions the workbench flow needs. Serves A1, A12.

### D2: Typed claude.env wrapping known Claude Code env vars
User decision. `claude.env` is a `ClaudeEnv` model: one validated field per known Claude Code env
var (field name = env var name lowercased; metadata: env name, group, secret, managed, description),
plus `extra` passthrough (uppercase names, may not shadow a modelled, managed or `CCW_*` var).
Global default in config, per-session override at create and resume (field-wise; `false`/`null`
unsets; `extra` merged key-wise). `CLAUDE_CONFIG_DIR` is catalogued but service-managed (set per
session; configuring it is a validation error naming `paths.sessions_root`). Booleans render `1`;
OAuth token wins over API key (key dropped with a warning); at most one provider flag. Secrets are
SecretStr and redacted in responses, logs, payloads and session.json. The implementer verifies the
catalogue against https://code.claude.com/docs/en/env-vars and the settings page and adds every other
documented, non-managed variable. README carries a full configuration reference generated-checked
against the models. Serves A9, A10.

### D3: Python stack and repo conventions
(my call) Python 3.12, uv, FastAPI + uvicorn, pydantic v2, ruff, pytest + pytest-asyncio,
`mise.toml` with `test`/`lint`, `.claude/workbench.json` suites, repo `CLAUDE.md`. Alternative:
Python 3.13 like magic; 3.12 per orchestrator default. Serves A1, A3.

### D4: Multi-session service with per-session dirs on two volumes
(my call) One process manages up to `sessions.max_sessions` sessions. Each session gets its own
workdir under `/workdir` and its own `CLAUDE_CONFIG_DIR` at `/sessions/<id>/config`, with
`CLAUDE_CODE_PROJECT_DIR_NAME=workdir` so the transcript path is deterministic
(`config/projects/workdir/<claude_session_id>.jsonl`); every hook payload's `transcript_path` overrides
the computed path when it differs. Alternatives: one shared config dir (concurrent `.claude.json`
writes by several claude processes), one session per container (tatara/magic). Destroy keeps files;
resume relaunches with `--resume <claude_session_id>` in the same config dir and workdir; after a
service restart persisted sessions list as `stopped`. Serves A5, A11.

### D5: SSE plus polling, no WebSocket
(my call) Per-session SSE stream with replay from `Last-Event-ID`/`after_seq` (default replay last
`limits.sse_replay`), heartbeats, `session_closed` at stop; polling via history, turns and transcript.
Serves A11.

### D6: Hook model: command and webhook actions per event
(my call) `hooks.<event>` is a list of actions; per-action timeout, `blocking`, `on_error`
(`allow`/`deny`), tool `matcher`; command gets the payload on stdin and `CCW_*` env, exit 2 denies;
webhook POSTs JSON with optional HMAC-SHA256 signature and retries. Non-blocking actions are tracked
background tasks drained on shutdown. Payloads are redacted. Event set and veto semantics per "Hook
events". Serves A7.

### D7: Claude hooks relay to a loopback endpoint plus transcript tailing
(my call) settings.json registers `ccw-hook <Event>` for UserPromptSubmit, PreToolUse, PostToolUse,
PostToolUseFailure, Stop, SubagentStart and SubagentStop. The relay (stdlib only, fast start) posts
the raw payload to `127.0.0.1:<internal_port>/internal/hooks/<Event>` with the per-session token,
prints the returned stdout and exits with the returned code; on failure it exits 0 silently (Stop
never blocks claude), except PreToolUse with `on_error: deny` configured on a blocking
onAgentToolCall action, which prints a deny. Assistant text and thinking come from the transcript
tailer (main thread and subagent files). Alternative: tatara's tailer-only tool observation (no
gating possible). Serves A7.

### D8: config.yaml schema and loading
(my call) Sections per "Config schema", `${VAR}` / `${VAR:-default}` / `$$`, `extra="forbid"`,
every error with its dotted path, `ccw check-config`, exit 78 on invalid config. Serves A6.

### D9: Auth and secret handling
(my call) Public API: optional bearer token (`server.auth_token`, constant-time compare; empty =
open with a startup warning). Internal API: loopback bind plus per-session hook token. Claude auth:
`CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` (or provider vars) via `claude.env`, typically
interpolated from the container env. The claude child env is built explicitly (no service secrets).
Serves A11.

### D10: Container and compose
(my call) Multi-stage Dockerfile on python:3.12-slim-bookworm: bash, git, openssh-client, jq, curl,
build-essential, shellcheck, node 24, pinned `@anthropic-ai/claude-code` (ARG
`CLAUDE_CODE_VERSION`, default 2.1.304), mise (pinned, `MISE_DATA_DIR=/opt/mise`, shims on PATH,
`MISE_TRUSTED_CONFIG_PATHS=/workdir`), uv, ruff, gh, glab; app venv from `uv sync --frozen --no-dev`;
user `agent` uid 1000, `HOME=/home/agent`; `DISABLE_AUTOUPDATER=1`; healthcheck on /healthz;
`CMD ccw serve --config /etc/ccw/config.yaml`. Compose: service `ccw`, port `${CCW_PORT:-8080}`,
named volumes `workdir`, `sessions`, `mise` (third volume is my call: runtime mise installs survive
container recreation), config mount `${CCW_CONFIG:-./config.example.yaml}:/etc/ccw/config.yaml:ro`,
env passthrough for auth vars, `.env.example`. Serves A5.

### D11: Workbench compatibility
(my call within the user requirement) `examples/workbench.config.yaml` plus
`examples/docker-compose.workbench.yml`: workdir mode `mounted` at the fixed absolute path
`/workdir/workbench` (clone alternative with `submodules: true` shown), plugin marketplace
`szymonrychu-workbench` from the read-only mount `/opt/plugins/claude-infra-plugin`, install
`workbench@szymonrychu-workbench` (required), disable `superpowers@claude-plugins-official`, copy the
plugin `rules/` into the config dir, OAuth token auth, passthrough of `GH_TOKEN`, `GITLAB_TOKEN`,
`SSH_AUTH_SOCK`, `WORKBENCH_SCM_HOSTS`, `WORKBENCH_ROOT` via `claude.env.extra`, git identity,
`sessions.ask_user_question: forward`. AskUserQuestion (the approval gate tool) would render a TUI
picker the service cannot answer; with `forward` the PreToolUse relay denies it with a reason telling
the agent to ask in plain text and end the turn, and emits a `question` event carrying the
questions, so the HTTP client answers in the next turn. Unsolicited Stop hooks (background
Workflow/Monitor completions) become `origin: agent` turn records instead of being dropped. Serves A2.

### D12: Plugin and mise installation
(my call) Plugins install per session at create, into that session's `CLAUDE_CONFIG_DIR`, with the
real CLI (`marketplace list/add`, `plugin list/install -y --json`), idempotent, verified via `plugin
list --json`; required failures abort creation. mise: global tools rendered to
`mise.global_config_path` and installed once at service start; per-session `mise trust` +
`mise install` in the workdir when it has `mise.toml`/`.mise.toml`; `MISE_*` vars passed to the
claude child so the Bash tool sees the same tools. Alternative: build-time plugin seed dir
(`CLAUDE_CODE_PLUGIN_SEED_DIR`), rejected because config.yaml is mounted at runtime. Serves A6.

### D13: Boot and turn methodology from tatara and magic
(my call, from the sources) Boot dialog handlers (pattern -> keys, once per boot), ready markers or
quiescence, boot timeout with one relaunch, ring reset on relaunch, fatal patterns failing the turn,
`.claude.json` seed (`bypassPermissionsModeAccepted` too) with resolved-path trust; submit =
bracketed paste, `submit_delay_seconds`, separate CR, turn marker comment; completion from Stop hook
with stale rejection; interrupt = ESC, resolved by the transcript interrupt line or a second ESC
after `interrupt_settle_seconds`; stall is reported (`stall_suspected`), never fatal; optional hard
`turn_max_seconds`; crash relaunch with `--resume` only when the transcript exists, bounded by
`max_restarts`; a turn in flight when claude exits fails with `claude_exited` (no automatic
re-paste). Serves A1.

### D14: Prompts
(my call) `<config>/CLAUDE.md` = `prompts.global_claude_md` file content + `prompts.system_append`
(user-level instructions; nothing written into the workdir); named templates with `{{name}}`
placeholders (no clash with `${VAR}` interpolation), required/default variables, 422 on a missing
one; `prompts.initial_prompt` submitted as turn `origin: initial` after onCreate unless the create
request supplies its own prompt. `--append-system-prompt` stays available via `claude.extra_args`.
Serves A6.

### D15: Test-first with a fake claude and an e2e lifecycle test
User requirement ("follow plugin TDD methodology"). Every task is red-green-refactor. A scripted
fake `claude` (stdlib Python under `tests/fake_claude/`) emulates the TUI boot dialog, paste plus
separate CR, transcript JSONL at the real path shape, settings.json hooks (UserPromptSubmit,
PreToolUse with deny, PostToolUse, PostToolUseFailure, SubagentStart/Stop, Stop), ESC interrupt
without Stop, fatal text, `--session-id`/`--resume`, and `plugin` subcommands. E2E tests drive the
whole stack over real HTTP. Serves A3.

### D16: Documentation deliverables
User requirement. `docs/source-analysis.md` (one section per source project, feature mapping, not
adopted); README.md (every endpoint with method, path, bodies, status codes; every SSE event; every
hook with payload schema; full config reference; docker/compose; workbench usage), with tests that
fail when a route, event, hook, config key or env var is undocumented. Serves A4, A8, A10.

### D17: Local delivery
(orchestrator default) No remote: the epic ends with a local merge to main; no MR, CI or deploy.
Serves A1.

### D18: Prometheus metrics
(my call, both PTY sources expose them) `/metrics` with sessions, turns, durations, tool calls
(label cardinality clamped), hook actions, restarts and tokens. Unauthenticated like the other ops
routes. Serves A11.

### D19: DELETE keeps files unless purge=true
DELETE /v1/sessions/{id} keeps the session dir and the workdir by default; only ?purge=true deletes them, and a mounted workdir is never deleted. (user decision)

### D20: Forward AskUserQuestion and POST it to callbackUrl
AskUserQuestion is forwarded by default: the PreToolUse relay denies it with guidance and emits a question event, and the question event envelope is also POSTed (signed with server.callback_secret) to the turn's callbackUrl, else the session's callbackUrl. The API uses camelCase callbackUrl consistently in request bodies, turn records and snapshots. (user decision)

### D21: Third named volume mise
docker-compose.yml declares a third named volume mise mounted at /opt/mise next to workdir and sessions. (user decision)

### D22: onTurnComplete fires for every terminal turn state
onTurnComplete fires for every terminal turn state (completed, failed, interrupted, timeout) with state in its data; turn completion callbacks follow the same rule. Orchestrator call, the user did not object. (user decision)

## Research

| Lens | Ran or skipped | Adopted | Rejected |
|---|---|---|---|
| research-code-internal | ran: four briefs over tatara-new, magic-trainer, ros2, workbench + plugin | PTY method, relay to loopback, tailer, event log, tool gate, bounded shutdown (Source findings) | SDK backend (D1), S3 resume, operator stall policy |
| research-docs | ran: Claude Code docs (sessions, plugins, env vars, permission modes, devcontainer), mise docs | CLAUDE_CONFIG_DIR per session, CLAUDE_CODE_PROJECT_DIR_NAME, plugin CLI, mise trust/data dir | build-time plugin seed (D12), IS_SANDBOX root bypass (undocumented) |
| research-code-external | skipped: the analogues are the three in-house wrappers already surveyed | none | none |
| research-papers | skipped: no algorithmic core (process supervision and HTTP glue) | none | none |
| probes | ran: `claude --help`, `claude plugin ... --help`, `plugin list --json` shapes, binary strings for hook events and env vars on 2.1.304 | Constraints facts | DISABLE_NON_ESSENTIAL_MODEL_CALLS (absent from the binary) |

## Research inputs

| Source | Entry | Lands in |
|---|---|---|
| tatara brief | PTY driver: bracketed paste, separate CR, ESC interrupt, write lock, ring | pty-process, session-turns |
| tatara brief | boot recipe: claude.json seed, bypass dialog, quiescence, boot timeout | home-seed, session-boot |
| tatara brief | settings renderer: hooks, permissions deny list, enableAllProjectMcpServers | settings-render |
| tatara brief | MCP base + overlay merge | home-seed (mcpServers in .claude.json) |
| tatara brief | Stop hook + loopback endpoint, last_assistant_message, stale rejection | hook-relay, internal-api, session-turns |
| tatara brief | transcript tailer, subagent watcher, redaction | transcript-tailer, claude-env-model |
| tatara brief | turn API 409/503, probe, interrupt, delete, webhook HMAC | api-turns-events, hook-dispatcher |
| tatara brief | crash relaunch with resume guard, restart budget | session-boot |
| tatara brief | lifecycle hook runner mapped to onCreate/onTurn/onDestroy | hook-dispatcher, event-router, session-manager |
| tatara brief | graceful shutdown ordering | service-main |
| tatara brief | image: pinned claude, mise shims, non-root | container |
| tatara brief | metrics | metrics |
| tatara brief | cross-pod resume via --resume on a session volume | session-manager |
| tatara brief | account usage statusline feed | rejected: subscriber-only data, not part of any requested feature |
| tatara brief | outcome re-prompt, commit/push, workspace identity claim | rejected: domain flow, users implement with onTurnComplete/onDestroy hooks |
| tatara brief | metrics push to operator | rejected: /metrics is scraped |
| magic brief | two-listener split public/loopback | internal-api, service-main |
| magic brief | PTY close order | pty-process |
| magic brief | fatal needles | session-boot, session-turns |
| magic brief | turn marker in prompt | session-turns, transcript-tailer |
| magic brief | interrupt escalation (no Stop hook) | session-turns |
| magic brief | files written before submit with guards | workdir-init, api-turns-events |
| magic brief | OAuth wins over API key | claude-env-model |
| magic brief | stub claude + injectable seams | fake-claude |
| magic brief | per-session model/effort override | api-sessions, session-manager |
| magic brief | one-shot --print review route | rejected: D1 (no -p) |
| magic brief | handover marker hold | rejected: game-specific; onDestroy blocking hooks cover graceful handover |
| ros2 brief | 202 submit + event stream | api-turns-events |
| ros2 brief | event log: seq, ring, paging, rotation, subscribers | event-log |
| ros2 brief | exception-safe gate, explicit deny | event-router, hook-dispatcher |
| ros2 brief | PostToolUse additionalContext | event-router, hook-dispatcher |
| ros2 brief | explicit child env, DISABLE_AUTOUPDATER, ENABLE_CLAUDEAI_MCP_SERVERS | session-boot, claude-env-model |
| ros2 brief | bounded shutdown, timeout_graceful_shutdown | service-main |
| ros2 brief | pydantic config extra=forbid, exit 78 | config-models, service-main |
| ros2 brief | ClaudeSDKClient, in-process SDK MCP tools | rejected: D1 |
| ros2 brief | context policies, digests, thumbnails | rejected: domain-specific |
| workbench brief | workdir = tree root at fixed absolute path | workdir-init, workbench-example |
| workbench brief | plugin install from directory marketplace | plugins-and-mise, workbench-example |
| workbench brief | rules/CLAUDE.md into config dir | home-seed, workbench-example |
| workbench brief | image tool list | container |
| workbench brief | OAuth token auth, git identity, forge tokens | claude-env-model, workdir-init, session-boot |
| workbench brief | AskUserQuestion surfaced to the HTTP client | event-router, settings-render |
| workbench brief | repo conventions (mise.toml, workbench.json, CLAUDE.md, gitignore) | repo-scaffold |
| sdk-docs brief | CLAUDE_CONFIG_DIR on the sessions volume | session-manager, container |
| sdk-docs brief | resume by id | session-boot, session-manager |
| sdk-docs brief | plugin CLI and sync install | plugins-and-mise |
| sdk-docs brief | root refuses bypassPermissions | container |
| sdk-docs brief | mise install --locked, data dir outside HOME | plugins-and-mise, container |
| sdk-docs brief | do not parse JSONL (internal format) | rejected: the PTY method needs the transcript; tailer tolerates unknown lines and fixtures pin real shapes |
| user D2 | typed env catalogue, override, redaction, README config reference | claude-env-model, config-models, api-sessions, session-manager, readme-config |
| user | source analysis doc | source-analysis |

## Decision coverage

| Decision | Tasks |
|---|---|
| D1 PTY backend | pty-process, session-boot, session-turns, fake-claude |
| D2 typed claude.env | claude-env-model, config-models, session-manager, api-sessions, readme-config |
| D3 stack and conventions | repo-scaffold |
| D4 multi-session dirs | session-manager, session-boot, transcript-tailer, container |
| D5 SSE and polling | event-log, api-turns-events |
| D6 hook model | hook-dispatcher, event-router, config-models |
| D7 relay and tailing | hook-relay, settings-render, internal-api, transcript-tailer, event-router |
| D8 config schema | config-models, service-main |
| D9 auth and secrets | api-sessions, internal-api, session-boot, claude-env-model |
| D10 container and compose | container |
| D11 workbench compatibility | workbench-example, event-router, settings-render, workdir-init, home-seed |
| D12 plugins and mise | plugins-and-mise, session-manager, service-main |
| D13 boot and turn method | session-boot, session-turns, home-seed |
| D14 prompts | home-seed, session-manager, api-turns-events |
| D15 TDD and fake claude | fake-claude, e2e-lifecycle |
| D16 documentation | source-analysis, readme-api, readme-config |
| D17 local delivery | e2e-lifecycle |
| D18 metrics | metrics |
| D19 DELETE keeps files unless purge=true | session-manager, api-sessions |
| D20 Forward AskUserQuestion and POST it to callbackUrl | event-router, api-sessions, api-turns-events, session-turns, session-manager, readme-api, e2e-lifecycle |
| D21 Third named volume mise | container |
| D22 onTurnComplete fires for every terminal turn state | event-router, readme-api, e2e-lifecycle |

## Contracts

| Part | Owner | Consumers | Where defined |
|---|---|---|---|
| `claude-env` | claude-env-model | config-models, session-boot, session-manager, api-sessions, readme-config | `src/claude_code_wrapper/claude_env.py` |
| `redactor` | claude-env-model | hook-dispatcher, transcript-tailer, event-router, api-sessions | `src/claude_code_wrapper/redact.py` |
| `config-schema` | config-models | home-seed, settings-render, hook-dispatcher, workdir-init, plugins-and-mise, session-boot, workbench-example, readme-config | `src/claude_code_wrapper/config.py` |
| `fake-claude` | fake-claude | pty-process, transcript-tailer, plugins-and-mise, session-boot, session-turns, session-manager, e2e-lifecycle | `tests/fake_claude/fake_claude.py` |
| `hook-relay-protocol` | hook-relay | settings-render, event-router, internal-api | `src/claude_code_wrapper/hook_relay.py` |
| `hook-dispatcher` | hook-dispatcher | event-router, readme-api | `src/claude_code_wrapper/hooks.py` |
| `event-log` | event-log | event-router, api-turns-events, readme-api | `src/claude_code_wrapper/events.py` |
| `session` | session-boot | session-turns, event-router | `src/claude_code_wrapper/session.py` |
| `event-router` | event-router | session-manager, metrics | `src/claude_code_wrapper/router.py` |
| `session-manager` | session-manager | internal-api, api-sessions, api-turns-events, service-main | `src/claude_code_wrapper/manager.py` |
| `public-api` | api-sessions | api-turns-events, metrics, service-main, readme-api | `src/claude_code_wrapper/api/app.py` |
| `service-cli` | service-main | e2e-lifecycle, container | `src/claude_code_wrapper/service.py` |
| `container-image` | container | workbench-example, readme-config | `Dockerfile` |

No cross-epic contracts: this is the only epic in the repo and `ticket.py contract list` is empty.

## Review focus

- PTY write ordering and close order (pty-process, session-turns): paste and CR must be separate
  writes; SIGTERM group, close fd, SIGKILL; no reader-thread deadlock.
- Turn slot invariants (session-turns, event-router): every path frees the slot (Stop, interrupt
  with and without the transcript line, fatal pattern, exit, timeout); stale Stop hooks never end the
  wrong turn.
- Secret flow (claude-env-model, config-models, api-sessions, event-router, service-main): secrets
  never reach responses, SSE, hook payloads, logs or session.json; the claude child env carries no
  service secrets.
- Gate path (hook-relay, internal-api, event-router, hook-dispatcher): deny wins, exceptions become
  allow (or deny per `on_error`) and never break claude; relay exits 0 on failure.
- Workdir safety (workdir-init, session-manager): file writes stay in the workdir; purge never
  deletes a mounted workdir.
- README coverage tests actually walk the models and routes (not hand-kept lists).

## Left to the builder

- Internal class and helper names beyond the module and part names above.
- Exact log format fields, as long as logs are JSON lines and redacted.
- Pinned versions of mise, uv, gh, glab and node in the Dockerfile (pin each; note why).
- Additional Claude Code env vars found in the official docs (added to the catalogue, D2).
- SSE implementation (plain StreamingResponse or a small helper), within the frame format above.

## Assumptions and unknowns

- `CLAUDE_CODE_PROJECT_DIR_NAME=workdir` pins the transcript dir on the image's CLI version; the
  router also takes `transcript_path` from the UserPromptSubmit/Stop payloads, so a different dir is
  still followed. Confirmed by the container task's `claude --help` probes for flags; the env var
  name was found in the 2.1.304 binary.
- The bypass warning may already be suppressed by `bypassPermissionsModeAccepted`; the dialog
  handler stays either way (harmless when the dialog does not appear).
- Real Claude transcript line shapes: pinned by fixtures copied from tatara and magic test data
  (`W/internal/transcript/testdata/*.jsonl`, magic `tests/wrapper/fixtures/`).
- A real-Claude smoke needs a user token; it is not part of the automated suite. README documents a
  manual smoke recipe (create, one turn, destroy) for the user.

## Builder check settlements

Binding answers to the builder check (minor items, settled with the proposed answers; mirrored in
the task files):

1. `SessionRouter` exposes `.session`, `.event_log`, `.turns`; `SessionManager` has `started` and
   read-only `open_event_log(id)` / `open_turns(id)` for persisted (stopped) sessions; history, turns
   and SSE work on stopped sessions from disk (EventLog and TurnStore get `readonly=True`).
2. `Session(..., turn_store=...)` kwarg; `listener.bind(session)`; `Session.record_tool_call()`.
3. `metrics=None` kwarg on `SessionRouter`, `SessionManager` and `create_app` (metrics task).
4. `PromptError("text_or_template")` maps to 400 everywhere; other PromptError codes 422.
5. `PtyProcess.send_prompt(text, delay)` holds the write lock once: paste, sleep, CR.
6. Both ports may be 0; sockets bind first; `ServiceHandle(public_port, internal_port)` is set on
   ready; the internal URL is derived after bind.
7. Tests pass `FAKE_CLAUDE_*` via `claude.env.extra` or `claude.passthrough_env`.
8. One shared helper `relay_timeouts(cfg)` in `bootstrap/settings.py`:
   `hook_timeout = max(30, max blocking timeout on onAgentToolCall, onToolResult, onTurnComplete,
   onSubagentStart, onSubagentStop + 5)`, `CCW_GATE_TIMEOUT = attempt_timeout = (hook_timeout - 2) / 2`;
   the manager imports it, no duplicated formula.
9. The relay sends one `X-CCW-Delivery` id per invocation (same on retries); the internal API dedupes
   by it (bounded cache per session); every event except UserPromptSubmit uses the long per-attempt
   timeout with 2 attempts (UserPromptSubmit: 3 x 5 s).
10. On Stop the router awaits `tailer.flush()` before reading the turn marker and usage.
11. Both FastAPI apps set `docs_url`, `redoc_url`, `openapi_url` to None.
12. `HookDispatcher(..., config_dir=...)` exports `CCW_CLAUDE_CONFIG_DIR`; one dispatcher per session.
13. A plugin is installed/enabled when its id is present in `claude plugin list --json`.
14. Public session state vocabulary (snapshots and the `state` filter): `creating, ready, busy,
    stopped, failed, dead` (internal booting -> creating, stopping -> stopped).
15. `SessionListener` is a base class with no-op defaults.
16. Subagent transcripts: `<config>/projects/<encoded workdir>/<claude_session_id>/subagents/agent-<id>.jsonl`
    (with the pinned dir name: `projects/workdir/<claude_session_id>/subagents/`); fake and tailer agree.
17. `create` does not wait for the initial turn (tracked background submit); a veto or rejection of
    the initial turn emits an `error` event and the session stays ready.

Container ACs that run docker are mandatory (Docker 29.8.2 is available on the build machine).

## Open questions

- Q1: Keep session files and the workdir on DELETE by default, deleting them only with ?purge=true? [recommended: yes]
- Q2: Forward AskUserQuestion to the HTTP client as a question event by default (the agent asks in plain text and waits for the next turn) instead of denying it outright? [recommended: yes]
- Q3: Add a third named volume mise for runtime-installed tools next to workdir and sessions? [recommended: yes]

## Irreversible operations in scope

- `DELETE /v1/sessions/{id}?purge=true` deletes the session directory and a non-mounted workdir. It
  runs only on an explicit client request with `purge=true`; a mounted workdir is never deleted.
- The container task's compose smoke runs `docker compose -p ccw-smoke down -v`, which removes only
  the throwaway `ccw-smoke` project's volumes it created moments earlier.
