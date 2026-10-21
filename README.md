# claude-code-wrapper

An HTTP microservice that runs N long-lived interactive Claude Code sessions, each under a PTY in
its own workdir, and lets any client drive them with turns, an SSE event stream and user-defined
lifecycle hooks. Python package `claude_code_wrapper`, command `ccw`.

## Overview

`claude-code-wrapper` manages **sessions**. A session is one interactive `claude` process running
under a PTY (never the Agent SDK, never `claude -p`), so it gets exactly the harness a human gets:
system prompt, skills, plugin hooks, background Workflow and Monitor tasks. The service pastes
turns into the TUI with bracketed paste, learns that a turn ended from Claude's `Stop` hook (relayed
over loopback HTTP to the service), observes tool calls and subagents from the other Claude hooks,
and reads assistant text and thinking from the JSONL transcript. It never parses terminal output
for results; the PTY ring is only used for boot dialogs and fatal-error detection.

Session model:

- Each session has an id `ses_<12 hex>`, its own workdir (under `paths.workdir_root`, or a mounted
  absolute path) and its own `CLAUDE_CONFIG_DIR` under `paths.sessions_root`, so transcripts,
  plugins and trust survive restarts.
- A session has a public `state`: `creating` (booting or running create steps), `ready` (idle,
  accepts a turn), `busy` (a turn is running), `stopped` (destroyed or the service shut down;
  resumable), `failed` (a create or resume step failed) or `dead` (claude exited and will not be
  relaunched).
- Turns are strictly sequential: one running turn per session. A second submit gets `409 busy`.
- A turn has a state: `running`, then one of `completed`, `failed`, `interrupted`, `timeout`.
  A turn also has an `origin`: `client` (submitted over the API), `initial` (the configured
  initial prompt) or `agent` (an unsolicited Stop, for example a background task finishing).
- Every happening is appended to a per-session event log (`events.jsonl`), streamed over SSE and
  pageable over `history`. User hooks (command or webhook actions) fire on the same happenings.
- After a service restart, persisted sessions list as `stopped`; `POST /v1/sessions/{id}/resume`
  relaunches claude with `--resume`.

Callbacks: a session (`callbackUrl` in the create body) or an individual turn (`callbackUrl` in the
turn body) can name a URL that receives a signed POST of the event envelope when a turn reaches a
terminal state and when Claude asks a question. See [Callbacks](#callbacks).

## Installation

`ccw` is a normal Python package (Python >= 3.12). Installing it puts two commands on `PATH`:
`ccw` (the service CLI) and `ccw-hook` (the Claude Code hook relay the service wires into each
session's `settings.json`, by absolute path).

```bash
pip install claude-code-wrapper                         # from a package index
pip install git+https://github.com/szymonrychu/claude-code-wrapper   # straight from git
pip install .                                           # from a checkout
pipx install claude-code-wrapper                        # isolated, on PATH
uv tool install claude-code-wrapper                     # same, with uv

ccw --version
```

### Containerless host

Prerequisites on `PATH`: the `claude` CLI (`npm install -g @anthropic-ai/claude-code`, or the
binary named in `claude.binary`), `git`, and optionally `mise` for the `mise:` section.
`ccw serve` checks `claude` and `git` at startup and exits with a clear message if either is
missing.

```bash
ccw config example > config.yaml        # the bundled example config
export CCW_WORKDIR_ROOT=$HOME/ccw/workdir CCW_SESSIONS_ROOT=$HOME/ccw/sessions
export MISE_DATA_DIR=$HOME/.local/share/mise   # only when using mise
mkdir -p "$CCW_WORKDIR_ROOT" "$CCW_SESSIONS_ROOT"
ccw serve --config config.yaml
```

Container-specific paths all come from config. The example sets `paths.workdir_root` and
`paths.sessions_root` from `CCW_WORKDIR_ROOT` (default `/workdir`) and `CCW_SESSIONS_ROOT`
(default `/sessions`), `mise.trusted_paths` from `CCW_WORKDIR_ROOT`, and
`mise.global_config_path` under `$HOME`; the mise data directory is mise's own `MISE_DATA_DIR`.

### Custom Docker image

```dockerfile
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends git nodejs npm \
 && npm install -g @anthropic-ai/claude-code
RUN pip install --no-cache-dir claude-code-wrapper     # or: pip install /tmp/*.whl
CMD ["ccw", "serve"]
```

The repository's own `Dockerfile` builds a wheel in a builder stage (`uv build --wheel`) and
`pip install`s it into the runtime image.

## Trust model

This is an internal backend service. Run it cluster-internal only (for example in Kubernetes) and
never put it behind a public ingress.

- One wrapper instance per project or assignment.
- Callers holding the bearer token (`server.auth_token`) are fully trusted. They can run arbitrary
  prompts, which means arbitrary commands in the session workdir with the session's credentials.
- There is no workdir allowlist: a caller may name any existing absolute path as a mounted workdir
  (when `init.workdir.mode` is `mounted`) and may write any relative path inside the workdir with
  turn `files`.
- There is no callback host policy: `callbackUrl` may point at any `http` or `https` host the
  service can reach.
- The service must not be exposed to untrusted callers. If `server.auth_token` is empty the service
  refuses to start unless `server.allow_anonymous` is `true`, in which case authentication on `/v1`
  is disabled and a warning is logged at start.
- `/healthz`, `/readyz` and `/metrics` are unauthenticated by design (cluster-internal probes and
  Prometheus scrape); keep the port off untrusted networks. The compose file publishes it on
  `127.0.0.1` only.
- The internal hook API (`/internal/...`) is bound to `127.0.0.1` only and additionally requires a
  per-session token; it is not part of the client surface.

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

Modules (`src/claude_code_wrapper/`):

| Module | Role |
|---|---|
| `claude_env.py` | typed model of every Claude Code env var (`claude.env`), secrets redacted |
| `redact.py` | secret redaction for responses, events, payloads and logs |
| `config.py`, `interpolation.py` | `config.yaml` schema, `${VAR}` interpolation, strict validation |
| `ring.py`, `pty_proc.py` | PTY child process and the ANSI-stripped screen ring |
| `bootstrap/` | per-session `.claude.json`, `CLAUDE.md` and `settings.json` rendering |
| `hook_relay.py` | `ccw-hook`: stdlib relay from a Claude hook call to the internal API |
| `hooks.py` | user hook dispatcher: command and signed-webhook actions, callbacks |
| `events.py` | durable per-session event log with ring, paging and live fan-out |
| `transcript.py` | JSONL transcript tailer and subagent watcher |
| `workdir.py`, `plugins.py`, `tooling.py` | workdir init, plugin pre-install, mise tooling |
| `session.py`, `turns.py` | boot and turn state machine, turn records and the `turns.jsonl` store |
| `router.py` | per-session router: Claude hooks and transcript lines to events and user hooks |
| `prompts.py` | prompt templates and variable substitution |
| `manager.py` | multi-session lifecycle: create, resume, destroy, snapshots |
| `internal_api.py` | loopback-only hook relay app |
| `api/` | public app, bearer auth, error envelope and the session, turn and event routers |
| `metrics.py`, `service.py`, `cli.py` | Prometheus metrics, service startup and shutdown, the `ccw` CLI |

## API reference

Public API: served on `server.host:server.port` (default `0.0.0.0:8080`). Every `/v1` route needs
`Authorization: Bearer <server.auth_token>` (constant-time compare; an empty token is refused unless `server.allow_anonymous` is true).
`/healthz`, `/readyz` and `/metrics` need no auth. Bodies are JSON (`Content-Type:
application/json`); unknown body fields are rejected with `422 invalid_request`. All request and
response keys are `snake_case`, except `callbackUrl` which is camelCase everywhere (request
bodies, turn records, snapshots). Errors use the envelope in [Errors](#errors). Every response
is passed through the secret redactor.

Missing or wrong token on any `/v1` route: `401 unauthorized` (with `WWW-Authenticate: Bearer`).

### `POST /v1/sessions`

Create a session: prepare the workdir, render the config dir, install plugins, start claude, wait
for the prompt, fire `onCreate`. Returns when the session is ready.

Auth: bearer.

Request body (all fields optional):

| Field | Type | Meaning |
|---|---|---|
| `name` | string | free-form label; for workdir modes `empty` and `clone` also the workdir directory name |
| `workdir` | string | with `init.workdir.mode: mounted`, the existing absolute path to use; otherwise a workdir name under `paths.workdir_root` |
| `model` | string | overrides `claude.model` for this session |
| `effort` | string | overrides `claude.effort` for this session |
| `env` | object | partial `claude.env` override: typed fields (lowercased env var names, for example `anthropic_model`) plus `extra` (a map of uppercase names); `false` or `null` unsets a global value; `extra` merges key-wise |
| `prompt` | string | initial prompt text, submitted as a turn with `origin: initial` after `onCreate` |
| `template` | string | name of a configured prompt template to use as the initial prompt (instead of `prompt`) |
| `variables` | object of string | values for the template's `{{name}}` placeholders |
| `callbackUrl` | string | `http`/`https` URL that receives turn-terminal and question callbacks for this session (a turn's own `callbackUrl` wins) |
| `commands` | object | per-session override of `sessions.commands` (`agent`, `client`, `timeout_seconds`, `continue_prompt`, `allow_force`, `quiet_seconds`; all optional, unset fields inherit); unknown command names are rejected; stored with the session and reapplied on resume |

When neither `prompt` nor `template` is given, `prompts.initial_prompt` (if configured) is used.
Secret `env` overrides are never persisted; resend them on resume.

Response `201`:

```json
{
  "session_id": "ses_3f9a1c0b7d42",
  "claude_session_id": "6b0f6c52-93a3-4f2c-8b0e-5b1c5a8d0f11",
  "name": "review-pr-42",
  "state": "ready",
  "step": "ready",
  "error": null,
  "workdir": "/workdir/review-pr-42",
  "workdir_mode": "empty",
  "model": "sonnet",
  "effort": null,
  "created_at": "2026-10-20T09:00:00.120+00:00",
  "updated_at": "2026-10-20T09:00:14.882+00:00",
  "last_activity_at": "2026-10-20T09:00:14.882+00:00",
  "turns_completed": 0,
  "outstanding_subagents": 0,
  "current_turn_id": null,
  "stall_suspected_since": null,
  "last_fatal": null,
  "restarts": 0,
  "pid": 4121,
  "env": {"claude_code_oauth_token": "***", "extra": {"GH_TOKEN": "***"}},
  "callbackUrl": "http://orchestrator.internal/ccw"
}
```

Snapshot fields: `state` is one of `creating`, `ready`, `busy`, `stopped`, `failed`, `dead`;
`step` is the create/resume step last entered (on `failed`, the one that failed, with `error:
{"step", "message"}`); `outstanding_subagents` is the number of subagents started and not yet stopped (0 for stopped and persisted sessions); `current_turn_id` is the running turn; `last_fatal` is the last fatal
screen text; `pid` is the claude pid while alive; `env` shows set values with secrets masked as
`***`.

Status codes:

| Status | Code | When |
|---|---|---|
| 201 | | session ready |
| 400 | `invalid_callback_url` | `callbackUrl` is not an `http` or `https` URL |
| 400 | `text_or_template` | both `prompt` and `template` given (initial prompt) |
| 401 | `unauthorized` | missing or wrong token |
| 403 | `vetoed` | a blocking `onCreate` action denied (the session is stopped) |
| 422 | `invalid_request` | unknown or invalid body field, invalid `env` override, unknown template or variable |
| 422 | `unknown_template`, `unknown_variable`, `missing_variable` | template problems |
| 429 | `too_many_sessions` | `sessions.max_sessions` reached |
| 500 | `create_failed` | a create step failed; the message names the step |

### `GET /v1/sessions`

List active and persisted sessions.

Auth: bearer. Query parameter: `state` (optional; one of `creating`, `ready`, `busy`, `stopped`,
`failed`, `dead`) filters by public state; any other value is `422 invalid_request`.

Response `200`: `{"sessions": [<snapshot>, ...]}` (snapshots as in `POST /v1/sessions`).

### `GET /v1/sessions/{id}`

One session snapshot (env secrets masked).

Auth: bearer. Path parameter: `id`, the session id.

Response `200`: the snapshot. `404 not_found` for an unknown session; `401 unauthorized`.

### `DELETE /v1/sessions/{id}`

Stop the session (bounded SIGTERM, then kill), fire `onDestroy` and close the event stream. Files
are kept by default so the session stays resumable.

Auth: bearer. Path parameter: `id`. Query parameter: `purge` (boolean, default `false`): when
true, the session directory and a non-mounted workdir are deleted afterwards; a mounted workdir is
never deleted.

Request body (optional): `{"hook_input": <object>}`, no other keys. It reaches `onDestroy` as
`data.input` (`null` when there is no body, and on a SIGTERM shutdown). A body over 1 MiB is
rejected with `413 too_large`.

Response `200`: the stopped snapshot; with `purge=true` it also carries `"purged": true`. It also
carries `hook_outputs`: `[{index, ok, output, error}]`, one entry per blocking `onDestroy` action
(the `output` the action returned); `hook_outputs` collects only blocking `onDestroy` actions. Non-blocking actions' outputs are not returned. `hook_outputs`
is `[]` when there are no blocking actions or the session was not active. The same list is sent as
the [`destroy_output`](#destroy_output) event.

Timing: `onDestroy` runs after claude stopped and before any purge, and `DELETE` returns after it.
On `DELETE` each action is bounded by its own `timeout_seconds`. On SIGTERM the service gives
`onDestroy` 25% of `limits.shutdown_timeout_seconds`, so a long review (for example a `claude -p`
action) must be triggered with `DELETE`, not left to shutdown.

| Status | Code | When |
|---|---|---|
| 200 | | stopped (idempotent on an already stopped session) |
| 401 | `unauthorized` | missing or wrong token |
| 404 | `not_found` | unknown session |
| 409 | `invalid_state` | the session is still `creating` |
| 413 | `too_large` | body over 1 MiB |
| 422 | `invalid_request` | `purge` is not a boolean, or the body is not `{"hook_input": object}` |

### `POST /v1/sessions/{id}/resume`

Relaunch a stopped session with `--resume <claude_session_id>` in the same config dir and workdir
and fire `onResume`.

Auth: bearer. Path parameter: `id`. Request body (optional): `{"env": {...}}`, a partial
`claude.env` override applied over the persisted non-secret one. Secret overrides are never
persisted, so a session created with them must have them resent here.

Response `200`: the ready snapshot.

| Status | Code | When |
|---|---|---|
| 200 | | resumed |
| 401 | `unauthorized` | missing or wrong token |
| 403 | `vetoed` | a blocking `onResume` action denied |
| 404 | `not_found` | unknown session |
| 409 | `not_resumable` | the session is not stopped, or has nothing to resume |
| 409 | `invalid_state` | the manager is shutting down |
| 422 | `invalid_request` | invalid `env` override |
| 429 | `too_many_sessions` | `sessions.max_sessions` reached |
| 500 | `create_failed` | a resume step failed |

### `POST /v1/sessions/{id}/turns`

Submit a turn: paste the prompt into the TUI and return at once. Completion arrives as
`turn_completed` or `turn_failed` events (and callbacks); poll `GET .../turns/{turn_id}` or follow
the SSE stream.

Auth: bearer. Path parameter: `id`.

Request body (exactly one of `text` and `template`):

| Field | Type | Required | Meaning |
|---|---|---|---|
| `text` | string | one of text/template | the prompt |
| `template` | string | one of text/template | name of a configured prompt template |
| `variables` | object | no | values for the template's `{{name}}` placeholders; only with `template` |
| `files` | object (path to content) | no | files written into the workdir before the prompt is pasted; relative paths only, limited by `limits.max_files_per_turn` and `limits.max_file_bytes` |
| `callbackUrl` | string | no | `http`/`https` URL that receives this turn's terminal-state callback (and its questions); overrides the session's `callbackUrl` |
| `metadata` | object | no | client-supplied JSON object kept on the turn record (`metadata`) and given to `onTurn` hooks as `data.metadata`, so hooks can gate on it; at most 16 KiB serialised |

Response `202`:

```json
{"turn_id": "turn-1k9x2m0q4zw8", "state": "running"}
```

| Status | Code | When |
|---|---|---|
| 202 | | accepted |
| 400 | `text_or_template` | both or neither of `text` and `template` |
| 400 | `invalid_callback_url` | `callbackUrl` is not an `http` or `https` URL |
| 400 | `invalid_path` | a `files` path is absolute, escapes the workdir or conflicts with an existing entry |
| 401 | `unauthorized` | missing or wrong token |
| 403 | `vetoed` | a blocking `onTurn` action denied (a `turn_rejected` event is logged) |
| 404 | `not_found` | unknown session |
| 409 | `busy` | a turn is already running |
| 413 | `too_large` | prompt over `limits.max_prompt_bytes`, `metadata` over 16 KiB, or `files` over the per-file or count limits |
| 422 | `invalid_request` | invalid body (including `metadata` that is not an object), or `unknown_template`, `unknown_variable`, `missing_variable` |
| 503 | `booting` | the session is still booting |
| 503 | `dead` | the session is stopped, failed or dead |

Crash during a turn: when claude exits mid-turn it is relaunched (with `--resume` when the
transcript exists) within `sessions.max_restarts`. By default the turn fails with `claude_exited`
and is never re-pasted; the client decides whether to retry. With
`sessions.restart_continue_prompt` set and restarts left, the turn stays `running` instead (no
`turn_failed`, no `onTurnComplete`, no callback; the session shows `creating` while claude
relaunches, then `busy`). Once the relaunch is resumed, the continue prompt is pasted with the
same `<!-- ccw turn: <turn_id> -->` marker, a `turn_resumed` event is logged, and the turn
completes on that claude's Stop as usual, with `usage` summed across the crash. The turn fails with
`claude_exited` exactly once when the restart budget is exhausted (the session is `dead`), the
relaunch fails to boot, or the relaunch is fresh (no transcript, so nothing to continue).
`limits.turn_max_seconds` and `sessions.turn_stall_seconds` keep measuring from the original
submit across the crash; an interrupt, `stop` or `DELETE` during the relaunch ends the turn
(`interrupted`, `timeout` or `session_stopped`) and nothing is pasted afterwards.

### `GET /v1/sessions/{id}/turns`

List the session's turns in start order.

Auth: bearer. Path parameter: `id`.

Response `200`:

```json
{"turns": [{
  "turn_id": "turn-1k9x2m0q4zw8", "origin": "client", "state": "completed",
  "started_at": "2026-10-20T09:01:00.010+00:00",
  "completed_at": "2026-10-20T09:01:09.455+00:00", "stop_reason": "end_turn"
}]}
```

Errors: `401 unauthorized`, `404 not_found` (unknown session).

### `GET /v1/sessions/{id}/turns/{turn_id}`

The full turn record.

Auth: bearer. Path parameters: `id`, `turn_id`.

Response `200`:

```json
{
  "turn_id": "turn-1k9x2m0q4zw8",
  "session_id": "ses_3f9a1c0b7d42",
  "origin": "client",
  "state": "completed",
  "text": "List the files in the repo.",
  "final_text": "The repo contains README.md and src/.",
  "stop_reason": "end_turn",
  "usage": {"input_tokens": 120, "output_tokens": 48,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
  "model_tokens": {"claude-sonnet-4-5": {"input_tokens": 120, "output_tokens": 48,
                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
  "error": null,
  "callbackUrl": "http://orchestrator.internal/ccw/turns",
  "started_at": "2026-10-20T09:01:00.010+00:00",
  "completed_at": "2026-10-20T09:01:09.455+00:00",
  "last_activity_at": "2026-10-20T09:01:09.401+00:00",
  "stall_suspected_since": null,
  "tool_calls": 2,
  "annotations": {"pending_tasks": 1},
  "metadata": {"handoff": true}
}
```

Fields: `state` is `running`, `completed`, `failed`, `interrupted` or `timeout`; `final_text` is
Claude's last assistant message (set on completed turns); `error` names why a turn failed
(`claude_exited`, `session_stopped`, or the fatal pattern matched on screen); `usage` sums token
counts across models, `model_tokens` breaks them down per model; `stall_suspected_since` is set
while no activity has been seen for `sessions.turn_stall_seconds` (reported, never fatal);
`tool_calls` counts tool results seen; `annotations` is the object hooks tagged the turn with
(`{}` when none did, see "Turn annotations" under Hooks); `metadata` is the object the client
sent on submit (`{}` when none, and for `initial` and `agent` turns).

Errors: `401 unauthorized`, `404 not_found` (unknown session or turn).

### `POST /v1/sessions/{id}/interrupt`

Interrupt the running turn: send ESC, resolve on the transcript's interrupt line, or send a second
ESC after `sessions.interrupt_settle_seconds`. The turn ends as `interrupted`, an `interrupted`
event is logged and `onInterrupt` fires.

Auth: bearer. Path parameter: `id`. No body.

Response `200`: `{"interrupted": true, "turn_id": "turn-1k9x2m0q4zw8"}`, or `{"interrupted": false,
"turn_id": null}` when no turn was running.

Errors: `401 unauthorized`, `404 not_found`, `503 dead` (the session is not running).

### `POST /v1/sessions/{id}/probe`

Write a liveness probe into the TUI without taking the turn slot (allowed while `busy`). The probe
asks Claude to reply with `sessions.probe_alive_token` (default `CCW-ALIVE`); it is `pending` until the transcript shows it was
delivered, `delivered` after, and `answered` once an assistant message containing the token
arrives; that main-chain text (first 2000 characters) is kept as the probe `answer`. State changes of an answered probe emit a `probe` event.

Auth: bearer. Path parameter: `id`. Request body (optional):

| Field | Type | Required | Meaning |
|---|---|---|---|
| `text` | string | no | probe text; the default asks Claude to reply with the configured token and continue |

Response `202`: `{"probe_id": "probe-1k9x2n7c1a5d", "state": "pending"}`.

Errors: `401 unauthorized`, `404 not_found`, `503 booting`, `503 dead`.

### `GET /v1/sessions/{id}/probe/{probe_id}`

Probe status.

Auth: bearer. Path parameters: `id`, `probe_id`.

Response `200`:

```json
{"probe_id": "probe-1k9x2n7c1a5d", "state": "answered",
 "created_at": "2026-10-20T09:02:00.100+00:00",
 "delivered_at": "2026-10-20T09:02:00.700+00:00",
 "answered_at": "2026-10-20T09:02:03.250+00:00",
 "answer": "CCW-ALIVE I am running the test suite."}
```

`answer` is `null` until the probe is answered.

Errors: `401 unauthorized`, `404 not_found` (unknown probe; probes are remembered in memory only),
`503 dead`.

### `GET /v1/sessions/{id}/transcript`

The session's main Claude transcript, one JSON object per line, with secrets redacted.

Auth: bearer. Path parameter: `id`.

Response `200`, `Content-Type: application/x-ndjson` (streamed). Errors: `401 unauthorized`,
`404 not_found` (unknown session), `404 no_transcript` (nothing written yet).

### `GET /v1/sessions/{id}/history`

Page through the session's event log (works for stopped sessions too).

Auth: bearer. Path parameter: `id`. Query parameters:

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `after_seq` | integer >= 0 | `0` | return events with `seq` greater than this |
| `limit` | integer >= 1 | `100` | page size, capped at 1000 |

Response `200`:

```json
{
  "events": [{"seq": 7, "ts": "2026-10-20T09:01:00.011Z", "session_id": "ses_3f9a1c0b7d42",
              "type": "turn_started",
              "data": {"turn_id": "turn-1k9x2m0q4zw8", "origin": "client", "text": "hi"}}],
  "next_seq": 7,
  "has_more": false
}
```

Pass `next_seq` as `after_seq` to fetch the next page while `has_more` is true. Errors:
`401 unauthorized`, `404 not_found`, `422 invalid_request` (bad query values).

### `GET /v1/sessions/{id}/events`

Server-sent events stream of the session's event log. Details, replay rules and a curl example
are in [Events (SSE)](#events-sse).

Auth: bearer. Path parameter: `id`. Query parameter: `after_seq` (integer >= 0; replay everything
after this seq). Header: `Last-Event-ID` (the seq of the last frame seen; wins over `after_seq`).

Response `200`, `Content-Type: text/event-stream`. Errors: `400 invalid_last_event_id` (header not
an integer), `401 unauthorized`, `404 not_found`, `422 invalid_request` (bad `after_seq`).

### `GET /v1/config`

The effective configuration with secrets masked as `***` (`server.auth_token`,
`server.callback_secret`, secret `claude.env` values, webhook secrets, MCP headers and secret
env).

Auth: bearer. Response `200`: the config object, one key per top-level `config.yaml` section.

### `GET /v1/prompts`

The configured prompt templates, sorted by name.

Auth: bearer. Response `200`:

```json
{"templates": [{"name": "review", "description": "Review a pull request",
                "variables": {"pr": {"required": true, "default": null}}}]}
```

### `GET /healthz`

Liveness. No auth. Response `200`: `{"status": "ok"}`.

### `GET /readyz`

Readiness: ready once config, global mise tooling and persisted-session loading finished. No auth.
Response `200`: `{"status": "ready"}`; `503 not_ready` before that.

### `GET /metrics`

Prometheus text exposition (`ccw_sessions_active`, `ccw_sessions_created_total`,
`ccw_turns_total`, `ccw_turn_duration_seconds`, `ccw_tool_calls_total`,
`ccw_hook_actions_total`, `ccw_claude_restarts_total`, `ccw_turn_tokens_total`,
`ccw_http_requests_total{route,method,status_code}` (route is the matched route template, or
`unmatched`), `ccw_http_panics_total` (unhandled handler exceptions),
`ccw_probe_outcomes_total{outcome}` (`sent`, `rejected`, `delivered`, `answered`),
`ccw_probe_answer_seconds`, `ccw_stall_suspected_total`,
`ccw_interrupts_total{result}` (`interrupted`, `timeout`, `idle`),
`ccw_commands_total{command,origin,result}` (`command` is a built-in command name, anything else
counts as `other`; `result` is the final state: `completed`, `failed`, `cancelled`),
`ccw_command_duration_seconds{command}` (histogram),
`ccw_compactions_total{trigger}` (`manual`, `auto`, `unknown`)). No auth.
Response `200`, `Content-Type: text/plain; version=0.0.4`.

### Internal API

Served on `127.0.0.1:server.internal_port` (default 8090) by a separate app, so a wrong bind
address can never expose it. It is used only by the `ccw-hook` relay that Claude's hooks call; it
is not a client API. Its errors are FastAPI's default `{"detail": "<message>"}` bodies, not the
public envelope.

### `POST /internal/hooks/{event}`

Receive a Claude hook call from `ccw-hook` and return what the relay must print and exit with.

Auth: loopback source address, plus headers `X-CCW-Session` (session id) and `X-CCW-Hook-Token`
(the per-session token, compared in constant time). `X-CCW-Delivery` (a uuid per hook call) makes
retries idempotent: a repeated delivery id returns the first result without re-running the
handlers.

Path parameter: `event`, a Claude hook event: `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
`PostToolUseFailure`, `Stop`, `SubagentStart`, `SubagentStop`, `PreCompact`, `PostCompact`. Request body: the raw JSON object
Claude passes the hook on stdin.

Response `200`:

```json
{"stdout": "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"deny\",\"permissionDecisionReason\":\"no writes\"}}",
 "stderr": "", "exit_code": 0}
```

| Status | When |
|---|---|
| 200 | handled (handler failures become an `error` event and an allow response) |
| 400 | body is not a JSON object |
| 403 | non-loopback caller, missing or wrong token, or unknown or stopped session |
| 404 | `event` is not a Claude hook event |

### `POST /internal/mcp`

The ccw MCP server (streamable HTTP, stateless, JSON responses only). It exposes one tool,
`command`, that lets the agent queue an allowlisted slash command (such as `compact` or
`reload-plugins`) which runs when it ends its turn, after which it is resumed automatically.

Auth: same as the hook route (loopback source address, `X-CCW-Session`, `X-CCW-Hook-Token`
compared in constant time); otherwise `403`. The body is one JSON-RPC 2.0 message: a JSON array
answers error `-32600`, invalid JSON `-32700`. A message without an `id` (a notification) answers
`202` with an empty body. `GET` and `DELETE /internal/mcp` answer `405`.

Methods: `initialize` (echoes the client's `protocolVersion` when it is `2025-11-25`,
`2025-06-18` or `2025-03-26`, else `2025-06-18`; returns `instructions`), `ping` (`{}`),
`tools/list`, `tools/call`. Every other method, including `server/discover`, answers `-32601`.

Tool `command`: `inputSchema` is `{name: enum of the session's agent allowlist, params?}` with
`additionalProperties: false` (the allowlist is `sessions.commands.agent`, per-session override
included). An empty allowlist lists no tools.

Success (`isError: false`) text: `Queued <id>. It runs when you end this turn; end your turn now
with a one-line status. You will be resumed automatically.`

Errors: a rejected command (unknown params, not allowed, queue full, force not allowed, invalid
state) returns `isError: true` with the reason; an empty allowlist returns `isError: true` "ccw
commands are disabled for this session"; any other tool name answers JSON-RPC error `-32602`.

### `GET /internal/healthz`

Loopback app liveness. Response `200`: `{"status": "ok"}`.

## Events (SSE)

Every event, in the log, in `history` pages and in SSE frames, has the same envelope:

```json
{"seq": 12, "ts": "2026-10-20T09:01:09.455Z", "session_id": "ses_3f9a1c0b7d42",
 "type": "turn_completed", "data": {}}
```

`seq` starts at 1 per session, strictly increases and continues across service restarts. `ts` is
UTC with milliseconds. `data` is described per type below; strings are redacted of secrets.

An SSE frame is:

```
id: 12
event: turn_completed
data: {"seq":12,"ts":"2026-10-20T09:01:09.455Z","session_id":"ses_3f9a1c0b7d42","type":"turn_completed","data":{...}}

```

Replay and delivery rules for `GET /v1/sessions/{id}/events`:

- `Last-Event-ID: <seq>` (sent automatically by browsers' `EventSource` on reconnect) replays every
  event after that seq, then goes live. A non-integer value is `400 invalid_last_event_id`.
- `?after_seq=<seq>` does the same when no `Last-Event-ID` header is sent.
- With neither, the last `limits.sse_replay` events (default 100) are replayed, then live events.
- A comment line `: ping` is sent every `limits.sse_heartbeat_seconds` (default 15) when idle.
- Events are never duplicated or missed between the replay and the live phase.
- A subscriber that falls more than 1000 events behind gets an `error` event with `data.message:
  "subscriber lagged"` and the stream ends; reconnect with `Last-Event-ID`.
- When the session is destroyed, a final `session_closed` event is sent and the stream ends. If the
  session was already closed, the replayed `session_closed` ends the stream.

```sh
curl -N -H "Authorization: Bearer $TOKEN" \
  -H "Last-Event-ID: 40" \
  http://localhost:8080/v1/sessions/ses_3f9a1c0b7d42/events
```

### `session_state`

Emitted on every state change of the session (`creating`, `ready`, `busy`, `stopped`, `failed`,
`dead`) and when claude is restarted.

`data`: `state` (string); `restart` (object `{count, resumed}`, only on a restart event).

```
event: session_state
data: {"seq":3,"ts":"2026-10-20T09:00:14.800Z","session_id":"ses_3f9a1c0b7d42","type":"session_state","data":{"state":"ready"}}
```

### `turn_started`

Emitted when a turn has been accepted and pasted.

`data`: `turn_id`, `origin` (`client`, `initial`, `agent`), `text` (the prompt).

```
event: turn_started
data: {"seq":7,"ts":"2026-10-20T09:01:00.011Z","session_id":"ses_3f9a1c0b7d42","type":"turn_started","data":{"turn_id":"turn-1k9x2m0q4zw8","origin":"client","text":"hi"}}
```

### `turn_rejected`

Emitted when a blocking `onTurn` hook vetoed a submit (the API answered `403 vetoed`).

`data`: `reason` (string).

```
event: turn_rejected
data: {"seq":8,"ts":"2026-10-20T09:01:02.000Z","session_id":"ses_3f9a1c0b7d42","type":"turn_rejected","data":{"reason":"quota exceeded"}}
```

### `turn_completed`

Emitted when the Stop hook ended a turn normally. Also POSTed to the turn's (else session's)
`callbackUrl`.

`data`: `record`, the full turn record (see `GET /v1/sessions/{id}/turns/{turn_id}`) with
`state: "completed"`.

```
event: turn_completed
data: {"seq":12,"ts":"2026-10-20T09:01:09.455Z","session_id":"ses_3f9a1c0b7d42","type":"turn_completed","data":{"record":{"turn_id":"turn-1k9x2m0q4zw8","session_id":"ses_3f9a1c0b7d42","origin":"client","state":"completed","text":"hi","final_text":"Hello!","stop_reason":"end_turn","usage":{"input_tokens":120,"output_tokens":48},"model_tokens":null,"error":null,"callbackUrl":null,"started_at":"2026-10-20T09:01:00.010+00:00","completed_at":"2026-10-20T09:01:09.455+00:00","last_activity_at":null,"stall_suspected_since":null,"tool_calls":0,"annotations":{},"metadata":{}}}}
```

### `turn_failed`

Emitted when a turn ends in any other terminal state: `failed` (claude exited, a fatal screen
pattern, session stopped), `interrupted` or `timeout` (`limits.turn_max_seconds`). Also POSTed to
the callback URL.

`data`: `record`, the full turn record; `record.state` is `failed`, `interrupted` or `timeout` and
`record.error` says why.

```
event: turn_failed
data: {"seq":15,"ts":"2026-10-20T09:03:00.000Z","session_id":"ses_3f9a1c0b7d42","type":"turn_failed","data":{"record":{"turn_id":"turn-1k9x2p1b8c0e","state":"failed","error":"claude_exited","...":"..."}}}
```

### `turn_continued`

Emitted when a blocking `onStop` action blocked the end of the running turn: claude was sent back
to work with the reason and the turn stays `running` (the session stays `busy`). No
`onTurnComplete` and no callback; the turn completes on a later allowed Stop.

`data`: `turn_id`, `reason` (what claude was told), `stop_hook_active` (true when this Stop
already followed an earlier block).

```
event: turn_continued
data: {"seq":10,"ts":"2026-10-20T09:01:05.000Z","session_id":"ses_3f9a1c0b7d42","type":"turn_continued","data":{"turn_id":"turn-1k9x2m0q4zw8","reason":"write outcome.json first","stop_hook_active":false}}
```

### `turn_deferred`

Emitted when the end of the running turn was deferred: claude was answered allow, but the turn
stays `running` and the session `busy`. `sessions.turn_stall_seconds`, `limits.turn_max_seconds`
and interrupt still apply to it.

`data`: `turn_id`, `reason`, `background_tasks` (the Stop payload's list, `[]` when absent),
`commands` (ids of the slash commands queued at that Stop, `[]` when none). `reason` is one of:

- `hook`: an `onStop` action answered `defer`; a later Stop that is allowed completes the turn.
- `background_tasks`: `sessions.defer_stop_on_background_tasks` and the Stop reported running
  background tasks; a later Stop with none completes the turn.
- `command`: the Stop was allowed while slash commands were queued. The commands listed in
  `commands` run now (`command_started`, `command_completed` or `command_failed`), then the
  agent is resumed in the same turn (`turn_resumed` with `cause: "command"`) and the next Stop
  completes it. Queued commands stay queued through a `hook` or `background_tasks` deferral and
  run at the later Stop that is not deferred.

```
event: turn_deferred
data: {"seq":11,"ts":"2026-10-20T09:01:06.000Z","session_id":"ses_3f9a1c0b7d42","type":"turn_deferred","data":{"turn_id":"turn-1k9x2m0q4zw8","reason":"background_tasks","background_tasks":[{"id":"b1","type":"local_bash","status":"running","description":"npm test"}],"commands":[]}}
```

### `turn_resumed`

Emitted when the running turn is continued with a prompt pasted with the turn's marker; the
session stays `busy` and the turn stays `running` until a later Stop completes the same record
(no second `turn_started`; `usage` sums every leg).

`data`: `turn_id`, `cause`, and per cause:

- `cause: "restart"`: a turn that was running when claude crashed is continued in the resumed
  relaunch with `sessions.restart_continue_prompt`. Also `restart` (the relaunch number, the
  session's `restarts` count). See "Crash during a turn" under `POST /v1/sessions/{id}/turns`.
- `cause: "command"`: the slash commands run at the turn's Stop (`turn_deferred` with
  `reason: "command"`) have finished. The continuation is `sessions.commands.continue_prompt`,
  a blank line and `Command results: <name> <state>[ <result or error>]; ...` (for example
  `Command results: compact completed ok; context failed timeout`). Also `commands` (the ids of
  the commands that ran, in order). An interrupt, `limits.turn_max_seconds` or a claude crash
  while the commands run ends the turn without this continuation: the running command gets ESC
  (none after a crash) and fails `interrupted`, `timeout` or `claude_exited`, the queued ones
  are `cancelled`, and after a crash the turn continues with `cause: "restart"` or fails as
  `claude_exited`.

```
event: turn_resumed
data: {"seq":14,"ts":"2026-10-20T09:02:10.000Z","session_id":"ses_3f9a1c0b7d42","type":"turn_resumed","data":{"turn_id":"turn-1k9x2m0q4zw8","restart":1,"cause":"restart"}}
```

### `assistant_text`

Emitted for each assistant text block read from the transcript (main thread and subagents).

`data`: `text`, `message_id`, `model`, `agent_id` (null for the main thread, the subagent id for a
sidechain; sidechain text does not fire `onAgentResponse`).

```
event: assistant_text
data: {"seq":10,"ts":"2026-10-20T09:01:05.000Z","session_id":"ses_3f9a1c0b7d42","type":"assistant_text","data":{"text":"Hello!","message_id":"msg_01","model":"claude-sonnet-4-5","agent_id":null}}
```

### `thinking`

Emitted for each thinking block read from the transcript.

`data`: `text`, `message_id`, `agent_id`.

```
event: thinking
data: {"seq":9,"ts":"2026-10-20T09:01:03.000Z","session_id":"ses_3f9a1c0b7d42","type":"thinking","data":{"text":"The user greets me.","message_id":"msg_01","agent_id":null}}
```

### `usage`

Emitted once per assistant `message_id` (the same dedupe as the per-turn usage totals) for
main-chain and subagent messages, so per-agent token use is derivable. Not emitted while a
resumed transcript's history is being dropped.

`data`: `message_id`, `model`, `agent_id` (null for the main agent), `input_tokens`,
`output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`.

```
event: usage
data: {"seq":10,"ts":"2026-10-20T09:01:05.000Z","session_id":"ses_3f9a1c0b7d42","type":"usage","data":{"message_id":"msg_01","model":"claude-sonnet-4-5","agent_id":null,"input_tokens":120,"output_tokens":48,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}
```

### `tool_call`

Emitted from the PreToolUse relay, before the tool runs (and before `onAgentToolCall` fires).

`data`: `tool_use_id`, `tool_name`, `tool_input` (as Claude sent it; a plugin hook that rewrites
Bash through `updatedInput` can make the executed command differ from this), `agent_id` and
`agent_type` (the subagent or teammate that acted; null for the main agent).

```
event: tool_call
data: {"seq":11,"ts":"2026-10-20T09:01:06.000Z","session_id":"ses_3f9a1c0b7d42","type":"tool_call","data":{"tool_use_id":"toolu_01","tool_name":"Bash","tool_input":{"command":"ls"},"agent_id":null,"agent_type":null}}
```

### `tool_result`

Emitted from the PostToolUse and PostToolUseFailure relays.

`data`: `tool_use_id`, `tool_name`, `is_error` (boolean), `response` (text, truncated when very
long), `agent_id`, `agent_type` (null for the main agent).

```
event: tool_result
data: {"seq":12,"ts":"2026-10-20T09:01:07.000Z","session_id":"ses_3f9a1c0b7d42","type":"tool_result","data":{"tool_use_id":"toolu_01","tool_name":"Bash","is_error":false,"response":"README.md\nsrc","agent_id":null,"agent_type":null}}
```

### `tool_denied`

Emitted when a blocking `onAgentToolCall` action denied a tool call; Claude receives the reason.

`data`: `tool_use_id`, `tool_name`, `reason`, `agent_id`, `agent_type`.

```
event: tool_denied
data: {"seq":13,"ts":"2026-10-20T09:01:07.500Z","session_id":"ses_3f9a1c0b7d42","type":"tool_denied","data":{"tool_use_id":"toolu_02","tool_name":"Write","reason":"read-only session"}}
```

### `question`

Emitted when Claude calls AskUserQuestion and `sessions.ask_user_question` is `forward` (the
default). The PreToolUse relay denies the tool with guidance ("ask in plain text, end your turn and
wait for the reply in the next message"), so the client answers by submitting the next turn. The
event envelope is also POSTed to the turn's `callbackUrl`, else the session's, signed with
`server.callback_secret` (see [Callbacks](#callbacks)). `tool_call` and `onAgentToolCall` are not
emitted for it.

`data`: `tool_use_id`, `questions` (the AskUserQuestion tool input: a `questions` list with
`question`, `header`, `options`, `multiSelect`).

```
event: question
data: {"seq":14,"ts":"2026-10-20T09:01:08.000Z","session_id":"ses_3f9a1c0b7d42","type":"question","data":{"tool_use_id":"toolu_03","questions":{"questions":[{"question":"Which branch?","header":"Branch","options":[{"label":"main","description":"default"},{"label":"dev","description":"integration"}],"multiSelect":false}]}}}
```

### `subagent_started`

Emitted from the SubagentStart relay.

`data`: `agent_id`, `agent_type`.

```
event: subagent_started
data: {"seq":16,"ts":"2026-10-20T09:01:10.000Z","session_id":"ses_3f9a1c0b7d42","type":"subagent_started","data":{"agent_id":"agent-1","agent_type":"Explore"}}
```

### `subagent_stopped`

Emitted from the SubagentStop relay.

`data`: `agent_id`, `agent_type`, `last_assistant_message` (when Claude supplied it).

```
event: subagent_stopped
data: {"seq":17,"ts":"2026-10-20T09:01:20.000Z","session_id":"ses_3f9a1c0b7d42","type":"subagent_stopped","data":{"agent_id":"agent-1","agent_type":"Explore","last_assistant_message":"Done."}}
```

### `interrupted`

Emitted when an interrupt request is accepted for the running turn (the turn then ends and a
`turn_failed` event with `state: interrupted` follows).

`data`: `turn_id`.

```
event: interrupted
data: {"seq":18,"ts":"2026-10-20T09:02:00.000Z","session_id":"ses_3f9a1c0b7d42","type":"interrupted","data":{"turn_id":"turn-1k9x2m0q4zw8"}}
```

### `stall_suspected`

Emitted once when the running turn shows no activity for `sessions.turn_stall_seconds`. Reported
only; the turn is not failed.

`data`: `turn_id`.

```
event: stall_suspected
data: {"seq":19,"ts":"2026-10-20T09:31:00.000Z","session_id":"ses_3f9a1c0b7d42","type":"stall_suspected","data":{"turn_id":"turn-1k9x2m0q4zw8"}}
```

### `probe`

Emitted when a liveness probe is answered.

`data`: the probe: `probe_id`, `state` (`answered`), `text`, `created_at`, `delivered_at`,
`answered_at`, `answer` (the answering assistant text, at most 2000 characters).

```
event: probe
data: {"seq":20,"ts":"2026-10-20T09:02:03.250Z","session_id":"ses_3f9a1c0b7d42","type":"probe","data":{"probe_id":"probe-1k9x2n7c1a5d","state":"answered","text":"Reply with CCW-ALIVE ...","created_at":"2026-10-20T09:02:00.100+00:00","delivered_at":"2026-10-20T09:02:00.700+00:00","answered_at":"2026-10-20T09:02:03.250+00:00","answer":"CCW-ALIVE I am running the test suite."}}
```

### `hook_result`

Emitted for every user hook action that ran (command or webhook), blocking or not.

`data`: `event` (hook event name), `index` (action index in the event's list), `type` (`command` or
`webhook`), `ok` (false when the action errored), `decision` (`allow`, `deny`, `block` or
`defer`, as honoured by the event), `duration_ms`,
`error` (null or the redacted failure), `output` (only when the action returned one), and
`invalid_decision` (only when the action returned a decision its event does not honour).

```
event: hook_result
data: {"seq":21,"ts":"2026-10-20T09:01:06.100Z","session_id":"ses_3f9a1c0b7d42","type":"hook_result","data":{"event":"onAgentToolCall","index":0,"type":"command","ok":true,"decision":"deny","duration_ms":41,"error":null}}
```

### `command_queued`

Emitted when a slash command is accepted into the session's command queue.

`data`: `{record}`, the CommandRecord fields (`command_id`, `name`, `args`, `origin`, `state`, `turn_id`, `error` and the timestamps).

### `command_started`

Emitted when the command is typed into Claude Code at an idle prompt.

`data`: `{record}`, the CommandRecord fields (`command_id`, `name`, `args`, `origin`, `state`, `turn_id`, `error` and the timestamps).

### `command_completed`

Emitted when the command finished successfully.

`data`: `{record}`, the CommandRecord fields (`command_id`, `name`, `args`, `origin`, `state`, `turn_id`, `error` and the timestamps).

### `command_failed`

Emitted when the command failed (including `vetoed` by an `onCommand` hook) or was cancelled.

`data`: `{record}`, the CommandRecord fields (`command_id`, `name`, `args`, `origin`, `state`, `turn_id`, `error` and the timestamps).

### `compacted`

Emitted once for every compaction, manual and auto alike, including Claude Code's own automatic ones.

`data`: `trigger` (`manual`, `auto` or `unknown`), `pre_tokens`, `post_tokens`, `duration_ms`.

### `error`

Emitted for fatal conditions and failures.

`data`: `message` (always) and, by origin: `kind` `fatal` (a fatal screen pattern such as `API Error:
401`; with `tail`, the last screen text), `exited` (claude exited; with `code`), `hook_relay` (a
Claude hook handler failed), `create_failed` or `resume_failed` (with `step`). A lagging subscriber
gets `{"message": "subscriber lagged"}` without a kind.

```
event: error
data: {"seq":22,"ts":"2026-10-20T09:05:00.000Z","session_id":"ses_3f9a1c0b7d42","type":"error","data":{"kind":"exited","message":"claude exited with code 1","code":1}}
```

### `destroy_output`

Emitted once per destroy, after `onDestroy` finished and before `session_closed`.

`data`: `outputs`: the `hook_outputs` list of blocking `onDestroy` actions (`[{index, ok, output,
error}]`, `[]` when none). Also emitted on a SIGTERM shutdown when `onDestroy` completes in time.

```
event: destroy_output
data: {"seq":22,"ts":"2026-10-20T09:09:59.000Z","session_id":"ses_3f9a1c0b7d42","type":"destroy_output","data":{"outputs":[{"index":0,"ok":true,"output":{"verdict":"pass"},"error":null}]}}
```

### `session_closed`

The last event of a session's stream: sent when the session is destroyed or the service stops.
The SSE stream ends after it.

`data`: empty object.

```
event: session_closed
data: {"seq":23,"ts":"2026-10-20T09:10:00.000Z","session_id":"ses_3f9a1c0b7d42","type":"session_closed","data":{}}
```

### Callbacks

`callbackUrl` can be set on the session (create body) and on a turn (turn body); the turn's wins.
The service POSTs the event envelope (`{seq, ts, session_id, type, data}`, the same JSON as in the
SSE `data:` line) to it in two cases:

- a turn reaches a terminal state: `type` is `turn_completed` for a completed turn and `turn_failed`
  for `failed`, `interrupted` and `timeout` (check `data.record.state`);
- Claude asks a question that was forwarded: `type` is `question`.

One receiver handles both and tells them apart by `type`. The POST has `Content-Type:
application/json`, `X-CCW-Delivery: <uuid>` and, when `server.callback_secret` is set,
`X-CCW-Signature: sha256=<hex HMAC-SHA256(callback_secret, raw body)>`. Delivery is best effort:
up to 3 retries with 0.5 s doubling backoff on connection errors and non-2xx responses, 10 s
timeout, then dropped with a warning (the event is still in the log). Any 2xx is success. Verify the
signature as shown under [Webhook actions](#webhook-actions).

## Hooks

Hooks are the extension point for domain logic: each configured event holds a list of actions that
run when it fires. Configure them under `hooks` in `config.yaml`:

```yaml
hooks:
  onTurn:
    - type: command
      run: ./check-quota.sh
      blocking: true        # awaited; a deny vetoes the turn
      on_error: deny        # action failed or timed out: deny (default: allow)
      timeout_seconds: 30
  onAgentToolCall:
    - type: command
      matcher: "Bash|Write"  # regex, full match on the tool name (tool events only)
      run: ./gate.sh
      blocking: true
  onTurnComplete:
    - type: webhook
      url: https://ci.internal/ccw
      secret: ${CCW_WEBHOOK_SECRET}
      headers: {X-Team: platform}
      retries: 3
      timeout_seconds: 10
```

Action fields: `type` (`command` or `webhook`), `blocking` (default `false`), `on_error` (`allow` or
`deny`, default `allow`; applies only to blocking actions), `matcher` (regex, valid only on
`onAgentToolCall` and `onToolResult`), `timeout_seconds` (command default 30, webhook default 10).
Command actions add `run` (a shell command, run with `sh -c` in the workdir) and `env`; webhook
actions add `url`, `secret`, `headers`, `retries` (default 3).

Semantics:

- Non-blocking actions run in the background and never delay anything; they are drained on shutdown.
- Blocking actions are awaited. Where the event can veto (`onCreate`, `onResume`, `onTurn`,
  `onAgentToolCall`), any blocking `deny` wins across actions. On other events `deny` is ignored.
  On `onStop` the strictest decision wins: `block` over `defer` over `allow`; the reason is the
  first such action's (index order).
- A blocking action that errors or times out follows its `on_error`; a non-blocking failure is
  only logged. Every action run emits a `hook_result` event.
- Payloads are redacted of secrets before delivery.

Payload envelope (command stdin, webhook body):

```json
{
  "event": "onTurn",
  "session_id": "ses_3f9a1c0b7d42",
  "claude_session_id": "6b0f6c52-93a3-4f2c-8b0e-5b1c5a8d0f11",
  "turn_id": "turn-1k9x2m0q4zw8",
  "ts": "2026-10-20T09:01:00.011Z",
  "workdir": "/workdir/review-pr-42",
  "data": {"text": "hi", "files": [], "origin": "client", "metadata": {}}
}
```

`turn_id` is the turn the event belongs to, else the running turn, else null. `data` per event is
below.

### Turn annotations

A blocking action may return `annotations` (an object). They are shallow-merged (later keys win)
into the running turn's record, which clients read as `annotations` in `GET .../turns/{turn_id}`
and in `turn_completed` / `turn_failed` events and callbacks. Only turn-scoped events annotate:
`onTurn` (applied when the turn starts), `onAgentToolCall`, `onToolResult`, `onStop` (every
leg, whether it allows, blocks or defers, so the values are in the `turn_completed` event and
callback) and `onTurnComplete` (merged into the terminal record and persisted again: visible in
`GET` turn, but the callback already sent is not re-sent). Annotations from any other event are
ignored. A merge that would take a turn's annotations above 16 KiB serialised is dropped and an
`error` event of kind `annotations_too_large` is emitted.

### `onCreate`

Trigger: the manager, after claude is ready on a new session. Blocking: a `deny` aborts creation
(the session is stopped; `POST /v1/sessions` answers `403 vetoed`). `data`: the session snapshot.

### `onResume`

Trigger: the manager, after a resumed claude is ready. Blocking: a `deny` aborts the resume
(`403 vetoed`). `data`: the session snapshot.

### `onDestroy`

Trigger: the manager, after claude stopped. Awaited with a bounded budget; cannot veto. `data`: the
stopped session snapshot plus `input`: the `hook_input` of the `DELETE` body (`null` when absent and
on shutdown). The `output` of each blocking action is returned in the `DELETE` response as
`hook_outputs` (non-blocking outputs are not returned).

### `onTurn`

Trigger: the router, before the prompt is pasted (also for `initial` and `agent` origins). Blocking:
a `deny` vetoes the turn (`403 vetoed`, `turn_rejected` event). `data`: `text`, `files` (list of
workdir-relative paths about to be written), `origin`, `metadata` (the submit body's object, `{}`
when absent).

### `onTurnComplete`

Trigger: every terminal turn state: the Stop relay for `completed`, and the router for `failed`,
`interrupted` and `timeout`. Cannot veto. `data`: the full turn record (same fields as
`GET /v1/sessions/{id}/turns/{turn_id}`), with `state` telling which terminal state was reached.
Turn callbacks (`callbackUrl`) follow the same rule.

### `onStop`

Trigger: Claude's Stop, relayed when it would end the running turn (a busy session, this
session's claude session id, the running turn's marker). A Stop with no turn running (a
background completion, recorded as an `origin: "agent"` turn) and a stale or foreign Stop fire no
`onStop`. Only the lead's Stop is gated: teammates fire `TeammateIdle`, not the parent Stop.
`data`: `turn_id`, `stop_hook_active` (true when this Stop follows a block), `last_assistant_message`,
`background_tasks`, `session_crons` (lists from the Stop payload, `[]` when absent),
`transcript_path` (null when absent).

Decisions (blocking actions; any other decision, `deny` included, is treated as `allow` and
recorded as `invalid_decision`). A blocking action that fails (crash, timeout, bad output) with
`on_error: deny` yields `block` with the failure reason; with `on_error: allow` it is an `allow`:

- `allow`: the turn completes as usual (one `turn_completed`, one `onTurnComplete`, one callback);
  its usage is summed over every leg of the turn. When slash commands are queued, an allowed Stop
  is deferred instead (`turn_deferred` with `reason: "command"`): claude is answered allow, the
  commands run once that answer is back with claude, and the turn resumes with the continue
  prompt (`turn_resumed`); the continuation's Stop fires `onStop` again. Pending commands gate
  the Stop this way even with no `onStop` action and no `defer_stop_on_background_tasks`.
- `block` with a `reason`: claude gets `{"decision": "block", "reason": R}` from the relay and
  keeps working on the same turn; the turn stays `running`, a `turn_continued` event is logged.
  Queued slash commands stay queued.
  A command action exiting `2` is a block with its trimmed stderr as the reason.
- `defer`: claude is answered allow, but the turn stays `running` and the session `busy`; a
  `turn_deferred` event (`reason: "hook"`) is logged. `turn_stall_seconds`,
  `limits.turn_max_seconds` and interrupt still end it; a later Stop that is allowed completes it.
  Queued slash commands stay queued until that Stop.

Claude caps consecutive Stop blocks at 8 per turn and then ends the turn regardless, so a gate
script must count its blocks itself, for example in a file under `CCW_STATE_DIR`, and allow
before the cap:

```sh
#!/bin/sh
# outcome-gate.sh: send claude back until outcome.json exists, at most 3 times per turn
payload=$(cat)
turn=$(printf '%s' "$payload" | jq -r '.turn_id')
count_file="$CCW_STATE_DIR/stop-blocks-$turn"
n=$(cat "$count_file" 2>/dev/null || echo 0)
if [ ! -f "$CCW_WORKDIR/outcome.json" ] && [ "$n" -lt 3 ]; then
  echo $((n + 1)) > "$count_file"
  echo "write outcome.json before you finish" >&2
  exit 2
fi
exit 0
```

`sessions.defer_stop_on_background_tasks: true` adds a built-in defer, decided before the hooks:
a Stop whose `background_tasks` is non-empty is at least a `defer`. A hook `allow` cannot lift it;
a hook `block` wins over it. With no `onStop` action and the flag off nothing changes.

### `onTick`

Trigger: a timer. The router starts one timer per `onTick` action when the session first reaches
ready; it keeps running across claude restarts and stops when the session is closed. Each tick
runs only its own action. Cannot veto: decisions, annotations and outputs are ignored, and
`onTick` is never relayed from claude. Two action keys are specific to this event and rejected on
every other one (the error names `hooks.onTick.<i>.<type>.interval_seconds`):

- `interval_seconds` (required, greater than 0): seconds between ticks.
- `when` (`busy` or `always`, default `busy`): `busy` skips ticks while no turn is running.

A tick whose previous run of the same action has not finished is skipped, so runs never overlap.
`data`: `turn_id` (the running turn, `null` when idle), `elapsed_seconds` (how long that turn has
run, `null` when idle), `interval_seconds`. Use it for periodic safety pushes or metrics pushes
without a sidecar.

```yaml
hooks:
  onTick:
    - {type: command, run: ./push-metrics.sh, interval_seconds: 30, when: always}
```

### `onAgentResponse`

Trigger: each main-thread assistant text block read from the transcript (subagent text excluded).
Cannot veto. `data`: `text`, `message_id`, `model`.

### `onAgentToolCall`

Trigger: Claude's PreToolUse, relayed before the tool runs. Supports `matcher`. Blocking: a `deny`
blocks the tool call (Claude is told the reason, a `tool_denied` event is logged); an action's
`additional_context` is returned to Claude. `data`: `tool_use_id`, `tool_name`, `tool_input`,
`agent_id`, `agent_type` (null for the main agent).
The gate fails closed: a ccw-side failure (relay error, unusable event, redaction failure) denies
the tool call when a matching blocking action has `on_error: deny`, with the reason
`ccw hook relay failed: <ExceptionType>`; with no such action the call is allowed and the failure
is reported as an `error` event.

### `onToolResult`

Trigger: PostToolUse and PostToolUseFailure. Supports `matcher`. Cannot veto; a blocking action's
`additional_context` is returned to Claude as hook context. `data`: `tool_use_id`, `tool_name`,
`is_error`, `response`, `agent_id`, `agent_type` (null for the main agent).

### `onSubagentStart`

Trigger: SubagentStart. Awaited when blocking; cannot veto. `data`: `agent_id`, `agent_type`.

### `onSubagentStop`

Trigger: SubagentStop. Awaited when blocking; cannot veto. `data`: `agent_id`, `agent_type`,
`last_assistant_message` (when provided).

### `onInterrupt`

Trigger: the router, when an interrupt request is accepted. Cannot veto. `data`: `turn_id`.

### `onCommand`

Trigger: a queued slash command is about to run. Awaited; can veto: a `deny` fails the command
with error `vetoed`. `data`: `command_id`, `name`, `args`, `origin`, `turn_id`.

### `onCommandComplete`

Trigger: a command reached its final state. Cannot veto. `data`: the final CommandRecord.

### `onError`

Trigger: fatal screen pattern, claude exit or restart, create or resume step failure, hook relay
failure. Cannot veto. `data`: `kind` (`fatal`, `exited`, `restart`, `hook_relay`, `create_failed`,
`resume_failed`), `message`, plus `tail` (fatal), `code` (exited), `count` (restart) or `step`
(create and resume failures).

### Command actions

The payload envelope is written to stdin. Environment: the FULL service environment (every
variable of the ccw process, including secrets such as `CCW_AUTH_TOKEN`), then the action's `env`,
then `CCW_EVENT`, `CCW_SESSION_ID`, `CCW_TURN_ID`, `CCW_WORKDIR`, `CCW_CLAUDE_CONFIG_DIR`,
`CCW_STATE_DIR` and `CCW_API_URL` (service-managed, they win over the action's `env`).
`CCW_STATE_DIR` is `<session_dir>/hook-state`, a per-session directory created before the first
hook (onCreate) fires, kept across stop and resume and removed by purge. `CCW_API_URL` is
`http://127.0.0.1:<public port>`. `CCW_CLAUDE_CONFIG_DIR` is the same path as the session's
`CLAUDE_CONFIG_DIR`. By contrast `init.setup_commands` get the allow-listed tool environment
(base vars, passthrough, claude env, mise, `CLAUDE_CONFIG_DIR`, `GIT_CONFIG_GLOBAL`), not the full
service environment. The working directory is the session workdir. The process runs in its own
process group and is killed with its children on timeout.

Exit codes: `0` allow; `2` deny (a block on `onStop`), with trimmed stderr (up to 2000 chars) as
the reason; any other exit code, or a timeout, is an error handled by `on_error`.

Stdout on exit 0 may be a JSON object; other output is ignored:

```json
{"decision": "deny", "reason": "writes are blocked", "additional_context": "Use the staging branch."}
```

`decision` is `allow`, `deny`, `block` or `defer`, `reason` is shown to Claude on a deny or block,
`additional_context` is appended to what Claude sees (for `onAgentToolCall` and `onToolResult`).
Two more optional fields, honoured on blocking actions only (a non-blocking action's are ignored):

- `annotations`: a JSON object. The blocking actions' annotations are shallow-merged in action
  index order (later keys win). Above 16 KiB serialised they are dropped and the action result
  records an error.
- `output`: any JSON value, redacted of secrets and then carried on the `hook_result` event and in
  the outcome's per-action `outputs` list. Above 256 KiB serialised it is replaced by
  `{"truncated": true, "bytes": <n>}`.

Decisions each event honours:

| Event | Honoured decisions |
|---|---|
| `onCreate`, `onResume`, `onTurn`, `onAgentToolCall` | `allow`, `deny` |
| `onStop` | `allow`, `block`, `defer` |
| `onTick` | `allow` (decisions are ignored) |
| every other event | `allow` |

A decision the event does not honour is treated as `allow`; the `hook_result` event records the
original as `invalid_decision` and it never vetoes. Without the new fields behaviour is unchanged.

```sh
#!/bin/sh
# gate.sh: block writes outside src/
payload=$(cat)
path=$(printf '%s' "$payload" | jq -r '.data.tool_input.file_path // empty')
case "$path" in
  "$CCW_WORKDIR"/src/*|"") exit 0 ;;
  *) echo "writes outside src/ are not allowed" >&2; exit 2 ;;
esac
```

### Webhook actions

The payload envelope is POSTed as JSON. Headers: `Content-Type: application/json`,
`X-CCW-Event: <hook event>`, `X-CCW-Delivery: <uuid>` (new per action run; retries reuse it), the action's own `headers`, and, when `secret` is set,
`X-CCW-Signature: sha256=<hex HMAC-SHA256(secret, raw request body)>`. A 2xx is success; a
connection error or non-2xx is retried `retries` times (default 3) with 0.5 s doubling backoff,
then counts as a failed action (`on_error` for blocking ones). A blocking webhook's 2xx JSON body
uses the same shape as command stdout: `decision`, `reason`, `additional_context`, `annotations`
and `output`, with the same per-event decision table and the same 16 KiB / 256 KiB caps (see
[Command actions](#command-actions)).

Verify the signature over the raw bytes, with a constant-time compare.

Python:

```python
import hashlib
import hmac


def verify(secret: str, raw_body: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)
```

Shell:

```sh
expected="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -hex | sed 's/^.* //')"
[ "$expected" = "$SIGNATURE_HEADER" ] || { echo "bad signature" >&2; exit 1; }
```

### How Claude hooks reach the service

The service renders `settings.json` in each session's `CLAUDE_CONFIG_DIR` so Claude calls the
`ccw-hook <Event>` relay for `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
`PostToolUseFailure`, `Stop`, `SubagentStart` and `SubagentStop`. The relay is stdlib-only Python
and does this:

1. Reads Claude's hook JSON from stdin.
2. POSTs it to `http://127.0.0.1:<server.internal_port>/internal/hooks/<Event>` (loopback only)
   with `X-CCW-Session`, the per-session `X-CCW-Hook-Token` (random, in memory and in the claude
   child's environment only; never in an API response, event or log) and a per-call
   `X-CCW-Delivery` id. Retries (3 attempts for `UserPromptSubmit`, 2 for the others) reuse that
   delivery id, so the service handles each call once.
3. Prints the returned `stdout` and `stderr` and exits with the returned `exit_code`.
4. If the service cannot be reached it exits 0 silently and never blocks Claude. The one exception:
   when any blocking `onAgentToolCall` action has `on_error: deny`, an unreachable service makes
   `PreToolUse` print a deny ("claude-code-wrapper is unreachable"), so the tool gate fails closed.

A plugin hook (for example the workbench plugin's) may rewrite a Bash command through
`updatedInput`. The `tool_input` in `tool_call` events and in `onAgentToolCall` payloads is what
Claude asked to run when the PreToolUse relay fired; the command that finally executes can differ.
Do not treat `tool_input` as proof of what ran.


## Hook examples

Worked examples of project behaviour expressed as hooks, with no project code in ccw. Each ships
under `src/claude_code_wrapper/data/examples/` (symlinked from `examples/`) and is tested against
the fake claude.

### tatara

[`tatara.config.yaml`](examples/tatara.config.yaml) and its script
[`tatara_hooks.py`](src/claude_code_wrapper/data/examples/tatara/tatara_hooks.py) (python stdlib
only, one script with subcommands) express tatara's turn path. Actions run
`${TATARA_PYTHON:-python3} ${TATARA_HOOKS:-/opt/tatara/tatara_hooks.py} <subcommand>`; the task
name comes from `TATARA_TASK`, the callback from `DEFAULT_CALLBACK_URL` signed with
`CALLBACK_HMAC_SECRET` (`X-Tatara-Signature: sha256=<hex>`), and `MAX_OUTCOME_REPROMPTS` (default
2, capped below claude's 8 Stop blocks) bounds the outcome gate. Tests:
[`tests/test_example_tatara.py`](tests/test_example_tatara.py).

| Parity row | Behaviour | Hook |
|---|---|---|
| B1 | Finaliser: commit and push changed repos, drain issues, add account usage, post the signed callback | `onTurnComplete`, non-blocking, `finalize` (a blocking finaliser would hold claude's Stop hook) |
| B2 | Internal issue capture, category and severity clamped (`other` / `medium`) | `onAgentToolCall`, blocking, matcher `mcp__tatara__report_internal_issue`, `capture-issue` |
| B3 | Remember whether the last outcome was rejected | `onToolResult`, non-blocking, matcher `mcp__tatara__submit_outcome`, `outcome-result` |
| B4 | Outcome gate: re-prompt a rejected outcome, counted in `CCW_STATE_DIR` | `onStop`, blocking, `outcome-gate` |
| B5 | Nothing posted while an outcome re-prompt is pending | `finalize` checks the gate's state |
| B6 | Account rate limits | `claude.settings.statusLine`, `statusline` writes `rate_limits.json`, read by `finalize` |

#### tatara lifecycle

The same config and script also carry tatara's lifecycle and pod policy (parity rows C1-C11, D1,
D4-D6, E1, A9). Tests: [`tests/test_example_tatara_lifecycle.py`](tests/test_example_tatara_lifecycle.py).

| Subcommand | Hook | Behaviour |
|---|---|---|
| `ttl-gate` | `onTurn`, blocking | The first turn records t0 in `$CCW_STATE_DIR/ttl.json`. Once `AGENT_POD_TTL_SECONDS` have passed, ordinary turns are denied with reason `pod ttl exceeded`, and exactly one turn whose `data.metadata.handoff` is `true` is admitted (submit it with `"metadata": {"handoff": true}`). Unset or `0` disables the gate. |
| `safety-push` | `onTick`, `interval_seconds: ${BRANCH_PUSH_INTERVAL_SECONDS:-120}`, `when: busy`, non-blocking | `git push origin $TASK_BRANCH` (else `HEAD`) in every repo under the workdir. Push only, never commit; a failing repo is reported on stderr and does not stop the others. |
| `metrics-push` | `onTick`, `interval_seconds: 30`, `when: always`, non-blocking | GETs `$CCW_API_URL/metrics` and POSTs the text to `OPERATOR_PUSH_URL` with the `RUN_ID` and `POD_NAME` headers. A no-op when `OPERATOR_PUSH_URL` is unset. |
| `bootstrap` | `init.setup_commands` | A skeleton of tatara's Go bootstrap covering clone, task branch, directives and MCP config. Clones `TATARA_REPOS` (JSON list of `{url, branch}`) or `REPO_URL`/`REPO_BRANCH` into `<workdir>/<owner>/<repo>` (depth 1 unless `TATARA_WORKSPACE_FULL_CLONE` is `true`), checks out `TASK_BRANCH` (created from the base branch when the remote lacks it), writes the headless and auto-push directive blocks into `$CLAUDE_CONFIG_DIR/CLAUDE.md`, and writes `<workdir>/.mcp.json` from `TATARA_EXTRA_MCP_SERVERS` (JSON object). `GIT_TOKEN` authenticates https clones through `GIT_CONFIG_*` env, never argv. |

Setup commands run with the allow-listed tool env, so every name the bootstrap reads is listed in
`claude.passthrough_env` as explicit globs (`TATARA_*`, `CLI_OIDC_*`, `OIDC_ISSUER`, `RUN_ID`,
`GIT_TOKEN`, `MISE_ALWAYS_KEEP_DOWNLOAD`, ...), never `*`. `claude.passthrough_env_exclude`
(`CALLBACK_HMAC_SECRET`, `OPERATOR_PUSH_*`) keeps hook-only secrets out of claude and the tools;
hooks still see them because hook commands inherit the service env.

Pod policy in the same config: `sessions.exit_when_dead: true` (the service exits when claude is
dead for good, so the pod restarts), `sessions.probe_alive_token: TATARA-ALIVE`,
`sessions.restart_continue_prompt` (sent after a claude restart), `sessions.ask_user_question: deny`,
model, effort and permission mode from `${MODEL:-}`, `${EFFORT:-}` and
`${PERMISSION_MODE:-bypassPermissions}`, and tatara's deny list in `claude.permissions.deny`.

### magic-trainer

[`magic-trainer.config.yaml`](examples/magic-trainer.config.yaml) and its script
[`mtg_hooks.py`](src/claude_code_wrapper/data/examples/magic-trainer/mtg_hooks.py) (python stdlib
only, subcommands) express magic-trainer's harness behaviour. Actions run
`${MTG_PYTHON:-python3} ${MTG_HOOKS:-/opt/mtg/mtg_hooks.py} <subcommand>`; values come from the env
as `${VAR:-default}` and no secret lives in a file. Tests:
[`tests/test_example_magic_trainer.py`](tests/test_example_magic_trainer.py).

| Parity row | Behaviour | Hook or option |
|---|---|---|
| #5 | Seat pair completion: the turn stays open while the pair's background work runs | `sessions.defer_stop_on_background_tasks: true`; agent teams via `claude.env` (`${AGENT_TEAMS:-}`) and `claude.settings.teammateMode: in-process` |
| #9 | Teammates missing from the Stop payload's `background_tasks` (the R1 fallback) | `onStop`, blocking, `pending-tasks`: reads `data.transcript_path`, collects background Agent/Task and Bash tool uses and spawned teammates since the turn's last marker, minus their completion notification or `TaskStop`. Prints `{"decision": "defer", "annotations": {"pending_tasks": [...]}}` when any, else `allow` with `[]` |
| #13-#15 | Review of the finished work by a fresh claude | `onDestroy`, `blocking: true` (required: `hook_outputs` in the `DELETE` response collects only blocking `onDestroy` actions), `timeout_seconds: ${REVIEW_TIMEOUT_SECONDS:-4800}`, `review`: reads `data.input` `{prompt, model, effort}` (from the `DELETE` body `hook_input`), runs `${CLAUDE_BIN:-claude} -p --setting-sources "" --strict-mcp-config --model <model>` with the prompt on stdin in the session workdir and `CLAUDE_CONFIG_DIR=$CCW_CLAUDE_CONFIG_DIR`, prints `{"output": {"text": <stdout>}}`. Without input there is no review (`{"output": null}`). It runs after claude stopped and before any purge |
| #18, #19 | Narration of steps and replies | `onAgentToolCall` and `onAgentResponse`, non-blocking, `narrate`: appends `step tool=<name> agent=<agent_type or main>` or `say <first 80 chars>` to `${NARRATION_LOG:-/proc/1/fd/1}` |
| #21 | The pod ends with its only session | `sessions.exit_when_last_destroyed: true` |
| #22 | A dead claude stays dead | `sessions.max_restarts: 0` |
| #23 | No interactive questions | `sessions.ask_user_question: deny` |
| #24 | MCP limits | `claude.env`: `mcp_timeout`, `mcp_tool_timeout` and `max_mcp_output_tokens` from `${MCP_TIMEOUT_MS:-60000}`, `${MCP_TOOL_TIMEOUT_MS:-600000}` and `${MAX_MCP_OUTPUT_TOKENS:-50000}` |
| #25 | Git and agent setup | `init.setup_commands`: a git credential helper (`git-credential`, token file `${MTG_GIT_TOKEN_FILE:-/etc/mtg/git-token}`), `safe.directory /guides`, and the consumer's agent renderer `${MTG_AGENTS_RENDER:-true}` |

## Errors

Every public API error has the envelope:

```json
{"error": {"code": "busy", "message": "a turn is already running"}}
```

`message` is human-readable and redacted of secrets; match on `code`. The internal API uses
FastAPI's `{"detail": ...}` body instead.

| Code | Status | Meaning |
|---|---|---|
| `unauthorized` | 401 | missing or invalid bearer token |
| `vetoed` | 403 | a blocking `onCreate`, `onResume` or `onTurn` hook denied; the message is the hook's reason |
| `not_found` | 404 | unknown session, turn, probe or route |
| `no_transcript` | 404 | the session has no transcript yet |
| `method_not_allowed` | 405 | the route exists but not for this method |
| `busy` | 409 | a turn is already running in the session |
| `invalid_state` | 409 | the operation does not apply to the session's current state (for example delete while `creating`) |
| `not_resumable` | 409 | the session cannot be resumed |
| `invalid_callback_url` | 400 | `callbackUrl` is not an `http` or `https` URL |
| `invalid_last_event_id` | 400 | the `Last-Event-ID` header is not an integer |
| `invalid_path` | 400 | a turn `files` path is invalid, escapes the workdir or conflicts with an existing entry |
| `text_or_template` | 400 | exactly one of `text` and `template` is required (turns), or both initial prompts were given (create) |
| `unknown_template` | 422 | the named prompt template does not exist |
| `unknown_variable` | 422 | a variable is not declared by the template, or variables were sent without a template |
| `missing_variable` | 422 | a required template variable has no value or default |
| `invalid_request` | 422 | the body, query or path failed validation, or the merged `env` is invalid; the message lists `field: problem` entries |
| `too_large` | 413 | prompt over `limits.max_prompt_bytes`, or turn `files` over `limits.max_file_bytes` or `limits.max_files_per_turn` |
| `too_many_sessions` | 429 | `sessions.max_sessions` reached |
| `create_failed` | 500 | a create or resume step failed; the message names the step |
| `booting` | 503 | the session is still booting |
| `dead` | 503 | the session is stopped, failed or dead and cannot take input |
| `not_ready` | 503 | `/readyz` before startup finished |
| `http_error` | any | any other HTTP error status raised by the framework |

## Quick start

Needs Docker with compose, and either a Claude subscription OAuth token (`claude setup-token`) or an
API key.

```bash
export CLAUDE_CODE_OAUTH_TOKEN=...      # or: export ANTHROPIC_API_KEY=...
export CCW_AUTH_TOKEN=change-me         # REQUIRED: bearer token; compose refuses to start without it
docker compose up -d --build            # uses ./config.example.yaml; returns once /healthz is ok
```

Create a session, send a turn, follow the events, destroy:

```bash
H="Authorization: Bearer $CCW_AUTH_TOKEN"
SID=$(curl -fsS -X POST localhost:8080/v1/sessions -H "$H" -H 'Content-Type: application/json' \
        -d '{"name":"demo"}' | jq -r .session_id)

curl -fsS -X POST localhost:8080/v1/sessions/$SID/turns -H "$H" -H 'Content-Type: application/json' \
     -d '{"text":"List the files in the workdir and explain what you see."}'

curl -N localhost:8080/v1/sessions/$SID/events -H "$H"        # SSE; Ctrl-C to stop following

curl -fsS -X DELETE localhost:8080/v1/sessions/$SID -H "$H"   # keeps files; add ?purge=true to delete them
```

See [API reference](#api-reference) for the exact bodies and [Events (SSE)](#events-sse) for the
stream. Without Docker see [Installation](#installation).

## Configuration reference

The service reads one YAML file: `--config <path>`, else `$CCW_CONFIG` (the image sets
`/etc/ccw/config.yaml`). Unknown keys are rejected everywhere. Every omitted key takes the default
listed below, so an empty file is a valid config.

**Interpolation.** Every string value in the file (keys excluded) is interpolated from the service
environment before validation:

| Form | Result |
|---|---|
| `${VAR}` | the value of `VAR`; an unset variable is a startup error naming the key |
| `${VAR:-default}` | the value of `VAR`, or `default` when `VAR` is unset or empty |
| `$$` | a literal `$` |

Interpolated values are strings and are then validated against the key's type, so
`port: ${PORT:-8080}` works for integers and booleans too.

**Validation.** The whole file is validated at start and every problem is reported as
`<dotted.path>: <message>`. An invalid config exits the process with status `78` (EX_CONFIG) and
the service does not start. Check a file without starting anything:

```bash
ccw check-config --config config.yaml     # prints the config as JSON with secrets masked as ***; exit 78 if invalid
```

In the tables, `Type` is the YAML type (`list` and `map` values describe their item shape in the
description), `Default` is the JSON rendering of the default, and `Env var` is `-` for every key
except `claude.env.*`, which map to the Claude Code environment variable named there. Keys of
nested sections are written by their full dotted path; object-valued parents (for example
`claude.boot`) have no row of their own, only their leaves do. List and map valued keys are
documented at their own path.

### `server`

```yaml
server:
  host: 0.0.0.0
  port: 8080
  internal_port: 8090
  auth_token: ${CCW_AUTH_TOKEN}
  allow_anonymous: false
  log_level: info
  callback_secret: ${CCW_CALLBACK_SECRET:-}
```

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `server.host` | string | `"0.0.0.0"` | - | Public API bind address. The internal hook API always binds 127.0.0.1. |
| `server.port` | integer | `8080` | - | Public API port, 0-65535 (0 picks a free port; used by tests). |
| `server.internal_port` | integer | `8090` | - | Loopback port of the hook relay API, 0-65535. Must differ from `server.port` unless 0. |
| `server.auth_token` | string | `""` | - | Bearer token required on `/v1`. Empty is refused at start unless `server.allow_anonymous` is true. Secret: masked as `***` by `ccw check-config`. |
| `server.allow_anonymous` | boolean | `false` | - | Permit an empty `server.auth_token`: `/v1` then runs unauthenticated and a warning is logged. `/healthz`, `/readyz` and `/metrics` are always unauthenticated. |
| `server.log_level` | enum (debug, info, warning, error, critical) | `"info"` | - | Log level of the JSON logs on stdout. |
| `server.callback_secret` | string | `""` | - | HMAC secret used to sign question callbacks (`callbackUrl`). Secret: masked. |

### `paths`

```yaml
paths:
  workdir_root: /workdir
  sessions_root: /sessions
```

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `paths.workdir_root` | string | `"/workdir"` | - | Absolute directory holding per-session workdirs (the `workdir` volume). |
| `paths.sessions_root` | string | `"/sessions"` | - | Absolute directory holding per-session state and `CLAUDE_CONFIG_DIR` (the `sessions` volume). |

### `claude`

```yaml
claude:
  binary: claude
  model: ""
  permission_mode: bypassPermissions
  extra_args: []
  env:
    claude_code_oauth_token: ${CLAUDE_CODE_OAUTH_TOKEN:-}
    anthropic_api_key: ${ANTHROPIC_API_KEY:-}
    disable_autoupdater: true
    extra:
      MY_VAR: value
  passthrough_env: [GH_TOKEN, SSH_AUTH_SOCK]
  permissions:
    allow: ["Bash(git status:*)"]
    deny: [EnterPlanMode, ExitPlanMode]
  settings: {}
  mcp_servers:
    remote:
      type: http
      url: https://mcp.example.com/mcp
      headers: {Authorization: "Bearer ${MCP_TOKEN:-}"}
  config_files:
    - {source: /etc/ccw/agents/x.md, target: agents/x.md}
  boot:
    timeout_seconds: 60
    dialogs:
      - {pattern: "Bypass Permissions mode", keys: ["\u001b[B", "\r"], delay_seconds: 0.6}
```

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `claude.binary` | string | `"claude"` | - | Claude Code executable to spawn. |
| `claude.model` | string | `""` | - | Value for `--model`; empty uses Claude's default. |
| `claude.effort` | string | `""` | - | Value for `--effort`; empty uses Claude's default. |
| `claude.permission_mode` | string | `"bypassPermissions"` | - | Written to `settings.json` as `permissions.defaultMode`. |
| `claude.extra_args` | list | `[]` | - | Extra argv appended after the service flags. `--dangerously-skip-permissions` is rejected (it adds a boot dialog). |
| `claude.passthrough_env` | list | `[]` | - | List of env var name patterns (`fnmatch`, case-sensitive; a plain name matches only itself, `GH_*` matches by prefix) copied from the service environment into Claude's environment and the setup-command environment when present. `CCW_*` names and service-managed names are never passed, even through `*` (the refusal is logged). Warning: `*` hands every service secret to Claude; use explicit globs plus `passthrough_env_exclude`. |
| `claude.passthrough_env_exclude` | list | `[]` | - | List of `fnmatch` patterns removing names matched by `claude.passthrough_env`, for example `["*_SECRET*", "AWS_*"]`. |
| `claude.permissions.allow` | list | `[]` | - | List of allow rules, for example `Bash(git status:*)`. |
| `claude.permissions.deny` | list | `["EnterPlanMode", "ExitPlanMode"]` | - | List of deny rules. Plan-mode tools are denied by default. |
| `claude.settings` | map | `{}` | - | Map deep-merged into `settings.json`; hook lists are appended to the service's own hooks. |
| `claude.claude_json` | map | `{}` | - | Map deep-merged into `.claude.json` in the session config dir. |
| `claude.mcp_servers` | map | `{}` | - | Map of server name to definition. Stdio: `{type: stdio, command, args: [], env: {}}`. Remote: `{type: http\|sse, url, headers: {}}`. Header values and secret-looking env values are masked. The name `ccw` is reserved: while `sessions.commands.agent` is non-empty (config or create override) the service injects its own `ccw` server (`${CCW_INTERNAL_URL}/internal/mcp`, with the session id and hook token passed as env references, so the token is never written to a file) and adds `mcp__ccw__command` to `permissions.allow`; a user entry named `ccw` is then a config error (a 422 at create for an override). |
| `claude.config_files` | list | `[]` | - | List of `{source, target}` files or directories copied into the session `CLAUDE_CONFIG_DIR`; `target` is relative without `..`. |
| `claude.boot.timeout_seconds` | number | `60` | - | Give up on boot after this many seconds (> 0). |
| `claude.boot.min_boot_seconds` | number | `4.0` | - | Never report ready sooner than this. |
| `claude.boot.idle_ready_seconds` | number | `1.5` | - | Output quiescence that counts as ready. |
| `claude.boot.submit_delay_seconds` | number | `0.4` | - | Gap between the bracketed paste and the Enter key. |
| `claude.boot.dialogs` | list | `[{"pattern": "Bypass Permissions mode", "keys": ["\u001b[B", "\r"], "delay_seconds": 0.6}, {"pattern": "Do you trust the files in this folder", "keys": ["\r"], "delay_seconds": 0.0}]` | - | List of `{pattern, keys: [], delay_seconds: 0.0}`: when `pattern` appears on screen, send `keys` after the delay. |
| `claude.boot.ready_markers` | list | `["? for shortcuts", "bypass permissions on"]` | - | List of screen texts that mark the TUI as ready. |
| `claude.boot.fatal_patterns` | list | `["API Error: 401", "API Error: 403", "Please run /login", "Credit balance is too low", "OAuth token has expired"]` | - | List of screen texts that fail the session (auth errors, no credit). |

The typed Claude Code environment. Each field is the variable name lowercased; unset fields are not exported. Full semantics: [Claude Code environment](#claude-code-environment).

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `claude.env.extra` | map | `{}` | - | Any other env var passed verbatim: a map of name to value. Names must match `[A-Z_][A-Z0-9_]*`, must not be modelled fields, `CLAUDE_CONFIG_DIR`, `CCW_*`, `TERM` or `HOME`. Values of secret-looking names (TOKEN, KEY, SECRET, PASSWORD, AUTH, CREDENTIAL) are masked. |
| `claude.env.anthropic_api_key` | secret | `null` | `ANTHROPIC_API_KEY` | API key sent as X-Api-Key header |
| `claude.env.anthropic_auth_token` | secret | `null` | `ANTHROPIC_AUTH_TOKEN` | Custom value for the Authorization header (the value you set here will be prefixed with Bearer ) |
| `claude.env.anthropic_aws_api_key` | secret | `null` | `ANTHROPIC_AWS_API_KEY` | Workspace API key for Claude Platform on AWS, generated in the AWS Console |
| `claude.env.anthropic_aws_base_url` | str | `null` | `ANTHROPIC_AWS_BASE_URL` | Override the Claude Platform on AWS endpoint URL. Use for custom regions or when routing through an LLM gateway |
| `claude.env.anthropic_aws_workspace_id` | str | `null` | `ANTHROPIC_AWS_WORKSPACE_ID` | Required for Claude Platform on AWS. Sent on every request as the anthropic-workspace-id header |
| `claude.env.anthropic_base_url` | str | `null` | `ANTHROPIC_BASE_URL` | Override the API endpoint to route requests through a proxy or gateway |
| `claude.env.anthropic_bedrock_base_url` | str | `null` | `ANTHROPIC_BEDROCK_BASE_URL` | Override the Amazon Bedrock endpoint URL. Use for custom Amazon Bedrock endpoints or when routing through an LLM gateway |
| `claude.env.anthropic_bedrock_mantle_base_url` | str | `null` | `ANTHROPIC_BEDROCK_MANTLE_BASE_URL` | Override the Amazon Bedrock Mantle endpoint URL. See Mantle endpoint |
| `claude.env.anthropic_bedrock_region_prefix` | str | `null` | `ANTHROPIC_BEDROCK_REGION_PREFIX` | Cross-region inference profile prefix (us, eu, apac, jp, au, or global) Claude Code tries first instead of the one de... |
| `claude.env.anthropic_bedrock_service_tier` | str | `null` | `ANTHROPIC_BEDROCK_SERVICE_TIER` | Amazon Bedrock service tier (default, flex, or priority) |
| `claude.env.anthropic_betas` | str | `null` | `ANTHROPIC_BETAS` | Comma-separated list of additional anthropic-beta header values to include in API requests |
| `claude.env.anthropic_custom_headers` | secret | `null` | `ANTHROPIC_CUSTOM_HEADERS` | Custom headers to add to requests (Name: Value format, newline-separated for multiple headers) |
| `claude.env.anthropic_custom_model_option` | str | `null` | `ANTHROPIC_CUSTOM_MODEL_OPTION` | Model ID to add as a custom entry in the /model picker |
| `claude.env.anthropic_custom_model_option_description` | str | `null` | `ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION` | Display description for the custom model entry in the /model picker |
| `claude.env.anthropic_custom_model_option_name` | str | `null` | `ANTHROPIC_CUSTOM_MODEL_OPTION_NAME` | Display name for the custom model entry in the /model picker |
| `claude.env.anthropic_custom_model_option_supported_capabilities` | str | `null` | `ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES` | Comma-separated list of capabilities the custom model supports, for example effort,thinking |
| `claude.env.anthropic_default_fable_model` | str | `null` | `ANTHROPIC_DEFAULT_FABLE_MODEL` | Model ID that the fable alias resolves to, and the ID Claude Code recognizes as a Fable model for automatic model fal... |
| `claude.env.anthropic_default_fable_model_description` | str | `null` | `ANTHROPIC_DEFAULT_FABLE_MODEL_DESCRIPTION` | Display description for the pinned Fable model in the /model picker |
| `claude.env.anthropic_default_fable_model_name` | str | `null` | `ANTHROPIC_DEFAULT_FABLE_MODEL_NAME` | Display name for the pinned Fable model in the /model picker |
| `claude.env.anthropic_default_fable_model_supported_capabilities` | str | `null` | `ANTHROPIC_DEFAULT_FABLE_MODEL_SUPPORTED_CAPABILITIES` | Comma-separated list of capabilities the pinned Fable model supports, for example effort,thinking |
| `claude.env.anthropic_default_haiku_model` | str | `null` | `ANTHROPIC_DEFAULT_HAIKU_MODEL` | Model ID that the haiku alias resolves to, also used for background functionality |
| `claude.env.anthropic_default_haiku_model_description` | str | `null` | `ANTHROPIC_DEFAULT_HAIKU_MODEL_DESCRIPTION` | Display description for the pinned Haiku model in the /model picker |
| `claude.env.anthropic_default_haiku_model_name` | str | `null` | `ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME` | Display name for the pinned Haiku model in the /model picker |
| `claude.env.anthropic_default_haiku_model_supported_capabilities` | str | `null` | `ANTHROPIC_DEFAULT_HAIKU_MODEL_SUPPORTED_CAPABILITIES` | Comma-separated list of capabilities the pinned Haiku model supports, for example effort,thinking |
| `claude.env.anthropic_default_model` | str | `null` | `ANTHROPIC_DEFAULT_MODEL` | Model that new sessions start on by default |
| `claude.env.anthropic_default_opus_model` | str | `null` | `ANTHROPIC_DEFAULT_OPUS_MODEL` | Model ID that the opus alias resolves to, and that opusplan uses while Plan Mode is active |
| `claude.env.anthropic_default_opus_model_description` | str | `null` | `ANTHROPIC_DEFAULT_OPUS_MODEL_DESCRIPTION` | Display description for the pinned Opus model in the /model picker |
| `claude.env.anthropic_default_opus_model_name` | str | `null` | `ANTHROPIC_DEFAULT_OPUS_MODEL_NAME` | Display name for the pinned Opus model in the /model picker |
| `claude.env.anthropic_default_opus_model_supported_capabilities` | str | `null` | `ANTHROPIC_DEFAULT_OPUS_MODEL_SUPPORTED_CAPABILITIES` | Comma-separated list of capabilities the pinned Opus model supports, for example effort,thinking |
| `claude.env.anthropic_default_sonnet_model` | str | `null` | `ANTHROPIC_DEFAULT_SONNET_MODEL` | Model ID that the sonnet alias resolves to, and that opusplan uses when Plan Mode is not active |
| `claude.env.anthropic_default_sonnet_model_description` | str | `null` | `ANTHROPIC_DEFAULT_SONNET_MODEL_DESCRIPTION` | Display description for the pinned Sonnet model in the /model picker |
| `claude.env.anthropic_default_sonnet_model_name` | str | `null` | `ANTHROPIC_DEFAULT_SONNET_MODEL_NAME` | Display name for the pinned Sonnet model in the /model picker |
| `claude.env.anthropic_default_sonnet_model_supported_capabilities` | str | `null` | `ANTHROPIC_DEFAULT_SONNET_MODEL_SUPPORTED_CAPABILITIES` | Comma-separated list of capabilities the pinned Sonnet model supports, for example effort,thinking |
| `claude.env.anthropic_federation_rule_id` | str | `null` | `ANTHROPIC_FEDERATION_RULE_ID` | Federation rule ID for Workload Identity Federation |
| `claude.env.anthropic_foundry_api_key` | secret | `null` | `ANTHROPIC_FOUNDRY_API_KEY` | API key for Microsoft Foundry authentication (see Microsoft Foundry) |
| `claude.env.anthropic_foundry_auth_token` | secret | `null` | `ANTHROPIC_FOUNDRY_AUTH_TOKEN` | Bearer token for Microsoft Foundry authentication, such as a Microsoft Entra access token |
| `claude.env.anthropic_foundry_base_url` | str | `null` | `ANTHROPIC_FOUNDRY_BASE_URL` | Full base URL for the Microsoft Foundry resource (for example, https://my-resource.services.ai.azure.com/anthropic) |
| `claude.env.anthropic_foundry_resource` | str | `null` | `ANTHROPIC_FOUNDRY_RESOURCE` | Microsoft Foundry resource name (for example, my-resource) |
| `claude.env.anthropic_model` | str | `null` | `ANTHROPIC_MODEL` | Name of the model setting to use (see Model Configuration) |
| `claude.env.anthropic_organization_id` | str | `null` | `ANTHROPIC_ORGANIZATION_ID` | Organization ID for Workload Identity Federation |
| `claude.env.anthropic_profile` | str | `null` | `ANTHROPIC_PROFILE` | Name of the Anthropic profile to authenticate with, such as one created by ant auth login or by signing in to a Conso... |
| `claude.env.anthropic_small_fast_model` | str | `null` | `ANTHROPIC_SMALL_FAST_MODEL` | [DEPRECATED] Name of Haiku-class model for background tasks |
| `claude.env.anthropic_small_fast_model_aws_region` | str | `null` | `ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION` | Override AWS region for the Haiku-class model when using Amazon Bedrock or Amazon Bedrock Mantle |
| `claude.env.anthropic_vertex_base_url` | str | `null` | `ANTHROPIC_VERTEX_BASE_URL` | Override Google Cloud's Agent Platform endpoint URL. Use for custom Google Cloud's Agent Platform endpoints or when r... |
| `claude.env.anthropic_vertex_project_id` | str | `null` | `ANTHROPIC_VERTEX_PROJECT_ID` | GCP project ID that Google Cloud's Agent Platform requests are addressed to |
| `claude.env.anthropic_workspace_id` | str | `null` | `ANTHROPIC_WORKSPACE_ID` | Workspace ID for workload identity federation |
| `claude.env.api_force_idle_timeout` | int | `null` | `API_FORCE_IDLE_TIMEOUT` | Override the 5-minute body idle timeout that aborts a streaming model response when no bytes arrive |
| `claude.env.api_timeout_ms` | int | `null` | `API_TIMEOUT_MS` | Time in milliseconds that each API request attempt has to receive its response headers (default: 600000, or 10 minute... |
| `claude.env.aws_bearer_token_bedrock` | secret | `null` | `AWS_BEARER_TOKEN_BEDROCK` | Amazon Bedrock API key for authentication (see Amazon Bedrock API keys) |
| `claude.env.aws_profile` | str | `null` | `AWS_PROFILE` | AWS profile name for Amazon Bedrock credentials |
| `claude.env.aws_region` | str | `null` | `AWS_REGION` | AWS region for Amazon Bedrock |
| `claude.env.bash_default_timeout_ms` | int | `null` | `BASH_DEFAULT_TIMEOUT_MS` | Default timeout for a foreground Bash or PowerShell tool command, in milliseconds (default: 120000, or 2 minutes) |
| `claude.env.bash_max_output_length` | int | `null` | `BASH_MAX_OUTPUT_LENGTH` | Maximum number of characters of bash output that Claude Code reads back into a command's result (default: 30000; maxi... |
| `claude.env.bash_max_timeout_ms` | int | `null` | `BASH_MAX_TIMEOUT_MS` | Maximum timeout the model can set for a foreground Bash or PowerShell tool command, in milliseconds (default: 600000,... |
| `claude.env.beta_tracing_endpoint` | str | `null` | `BETA_TRACING_ENDPOINT` | OTLP/HTTP endpoint for detailed beta tracing: with ENABLE_BETA_TRACING_DETAILED=1, logs and traces go there instead o... |
| `claude.env.ccr_force_bundle` | bool | `null` | `CCR_FORCE_BUNDLE` | Set to 1 to force claude --cloud to bundle and upload your local repository instead of cloning from its remote |
| `claude.env.claudecode` | str | `null` | `CLAUDECODE` | Set to 1 in subprocesses Claude Code spawns (Bash and PowerShell tools, tmux sessions, hook commands, status line com... |
| `claude.env.claude_afk_countdown_ms` | int | `null` | `CLAUDE_AFK_COUNTDOWN_MS` | How many milliseconds before auto-continue the on-screen countdown appears on an unanswered AskUserQuestion dialog |
| `claude.env.claude_afk_timeout_ms` | int | `null` | `CLAUDE_AFK_TIMEOUT_MS` | How many milliseconds of idle time before an unanswered AskUserQuestion dialog auto-continues without you |
| `claude.env.claude_agent_sdk_disable_builtin_agents` | bool | `null` | `CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS` | Set to 1 to disable all built-in subagent types such as Explore and Plan |
| `claude.env.claude_agent_sdk_mcp_no_prefix` | bool | `null` | `CLAUDE_AGENT_SDK_MCP_NO_PREFIX` | Set to 1 to skip the mcp__<server>__ prefix on tool names from SDK-created MCP servers |
| `claude.env.claude_async_agent_stall_timeout_ms` | int | `null` | `CLAUDE_ASYNC_AGENT_STALL_TIMEOUT_MS` | Stall timeout in milliseconds for subagents |
| `claude.env.claude_autocompact_pct_override` | int | `null` | `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` | Set the percentage (1-100) of the auto-compact window at which auto-compaction triggers |
| `claude.env.claude_auto_background_tasks` | bool | `null` | `CLAUDE_AUTO_BACKGROUND_TASKS` | Set to 1 to force-enable automatic backgrounding of long-running agent tasks |
| `claude.env.claude_ax_prepark_ms` | int | `null` | `CLAUDE_AX_PREPARK_MS` | In screen reader mode, how many milliseconds Claude Code waits before it writes a new or changed line |
| `claude.env.claude_ax_screen_reader` | bool | `null` | `CLAUDE_AX_SCREEN_READER` | Set to 1 to render screen-reader friendly output: flat text without decorative borders or animations |
| `claude.env.claude_ax_startup_quiet_ms` | int | `null` | `CLAUDE_AX_STARTUP_QUIET_MS` | In screen reader mode, how many milliseconds Claude Code holds the first interface render after the startup confirmat... |
| `claude.env.claude_bash_maintain_project_working_dir` | str | `null` | `CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR` | Return to the original working directory after each Bash or PowerShell command in the main session |
| `claude.env.claude_byte_stream_idle_timeout_ms` | int | `null` | `CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS` | Timeout in milliseconds for the byte-level streaming idle watchdog; when set, it takes precedence over CLAUDE_STREAM_... |
| `claude.env.claude_client_presence_file` | str | `null` | `CLAUDE_CLIENT_PRESENCE_FILE` | Path to a file that an external tool, such as a screen-lock listener, creates when you unlock your screen and deletes... |
| `claude.env.claude_code_accessibility` | bool | `null` | `CLAUDE_CODE_ACCESSIBILITY` | Set to 1 to keep the native terminal cursor visible and disable the inverted-text cursor indicator |
| `claude.env.claude_code_additional_directories_claude_md` | bool | `null` | `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD` | Set to 1 to load memory files from directories specified with --add-dir |
| `claude.env.claude_code_alt_screen_full_repaint` | bool | `null` | `CLAUDE_CODE_ALT_SCREEN_FULL_REPAINT` | Set to 1 to repaint the entire screen on every frame in fullscreen rendering instead of sending incremental updates |
| `claude.env.claude_code_always_enable_effort` | bool | `null` | `CLAUDE_CODE_ALWAYS_ENABLE_EFFORT` | Set to 1 to send the effort parameter with every request, even when Claude Code does not recognize the model ID as ef... |
| `claude.env.claude_code_api_key_helper_ttl_ms` | int | `null` | `CLAUDE_CODE_API_KEY_HELPER_TTL_MS` | Interval in milliseconds at which credentials should be refreshed (when using apiKeyHelper) |
| `claude.env.claude_code_artifact_auto_open` | str | `null` | `CLAUDE_CODE_ARTIFACT_AUTO_OPEN` | Set to 0 to stop Claude Code from opening the browser automatically when a new artifact is published |
| `claude.env.claude_code_artifact_comments` | str | `null` | `CLAUDE_CODE_ARTIFACT_COMMENTS` | Set to 0 to stop Claude reading and replying to comments on an artifact |
| `claude.env.claude_code_artifact_comments_autoreact` | str | `null` | `CLAUDE_CODE_ARTIFACT_COMMENTS_AUTOREACT` | Set to 0 to stop Claude replying on its own to comments sent to it |
| `claude.env.claude_code_artifact_publish_edits` | str | `null` | `CLAUDE_CODE_ARTIFACT_PUBLISH_EDITS` | Set to 0 to stop Claude from applying small changes to an artifact's file as part of publishing it |
| `claude.env.claude_code_attribution_header` | str | `null` | `CLAUDE_CODE_ATTRIBUTION_HEADER` | Set to 0 to omit the attribution block, which carries the client version and a prompt fingerprint, from the start of ... |
| `claude.env.claude_code_auto_background_worker_checkin_seconds` | str | `null` | `CLAUDE_CODE_AUTO_BACKGROUND_WORKER_CHECKIN_SECONDS` | Removed in v2.1.283. Use CLAUDE_CODE_WORKER_CHECKIN_SCHEDULE instead |
| `claude.env.claude_code_auto_compact_window` | str | `null` | `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | Set the auto-compact window, how full the context gets before compaction, from 100000 to 1000000 tokens |
| `claude.env.claude_code_auto_connect_ide` | str | `null` | `CLAUDE_CODE_AUTO_CONNECT_IDE` | Override automatic IDE connection |
| `claude.env.claude_code_auto_mode_model` | str | `null` | `CLAUDE_CODE_AUTO_MODE_MODEL` | Sets the model for the classifier requests Claude Code sends in auto mode |
| `claude.env.claude_code_auto_mode_server` | str | `null` | `CLAUDE_CODE_AUTO_MODE_SERVER` | Controls whether Claude Code asks the server to review auto mode actions |
| `claude.env.claude_code_aws_chain_resolve_timeout_ms` | int | `null` | `CLAUDE_CODE_AWS_CHAIN_RESOLVE_TIMEOUT_MS` | Time in milliseconds Claude Code waits for the AWS default credential provider chain to produce credentials before th... |
| `claude.env.claude_code_bash_edit_diff` | str | `null` | `CLAUDE_CODE_BASH_EDIT_DIFF` | Set to 0 to turn off the diff of the files that changed while a Bash command ran, or 1 to record it in every permissi... |
| `claude.env.claude_code_bg_tasks_report_running` | str | `null` | `CLAUDE_CODE_BG_TASKS_REPORT_RUNNING` | Set to 0 to make a non-interactive session report an idle status to its host at every turn end, even while background... |
| `claude.env.claude_code_bridge_session_id` | str | `null` | `CLAUDE_CODE_BRIDGE_SESSION_ID` | Set automatically in Bash tool and hook command subprocesses while the session has an active Remote Control connectio... |
| `claude.env.claude_code_bs_as_ctrl_backspace` | str | `null` | `CLAUDE_CODE_BS_AS_CTRL_BACKSPACE` | Set to 0 to make Claude Code read the 0x08 byte, also written ^H, as plain Backspace, or 1 to read it as Ctrl+Backspace |
| `claude.env.claude_code_cert_store` | str | `null` | `CLAUDE_CODE_CERT_STORE` | Comma-separated list of CA certificate sources for TLS connections |
| `claude.env.claude_code_child_session` | str | `null` | `CLAUDE_CODE_CHILD_SESSION` | Set to 1 in subprocesses Claude Code spawns via the Bash, PowerShell, and Monitor tools, hook commands, and status li... |
| `claude.env.claude_code_client_cert` | str | `null` | `CLAUDE_CODE_CLIENT_CERT` | Path to client certificate file for mTLS authentication |
| `claude.env.claude_code_client_key` | str | `null` | `CLAUDE_CODE_CLIENT_KEY` | Path to client private key file for mTLS authentication |
| `claude.env.claude_code_client_key_passphrase` | secret | `null` | `CLAUDE_CODE_CLIENT_KEY_PASSPHRASE` | Passphrase for encrypted CLAUDE_CODE_CLIENT_KEY (optional) |
| `claude.env.claude_code_connect_timeout_ms` | str | `null` | `CLAUDE_CODE_CONNECT_TIMEOUT_MS` | Removed in v2.1.186 and now a no-op |
| `claude.env.claude_code_debug_logs_dir` | str | `null` | `CLAUDE_CODE_DEBUG_LOGS_DIR` | Override the debug log file path |
| `claude.env.claude_code_debug_log_level` | str | `null` | `CLAUDE_CODE_DEBUG_LOG_LEVEL` | Minimum log level written to the debug log file |
| `claude.env.claude_code_disable_1m_context` | bool | `null` | `CLAUDE_CODE_DISABLE_1M_CONTEXT` | Set to 1 to turn off 1M context window support |
| `claude.env.claude_code_disable_adaptive_thinking` | bool | `null` | `CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING` | Set to 1 to disable adaptive reasoning on Opus 4.6 and Sonnet 4.6 and fall back to the fixed thinking budget controll... |
| `claude.env.claude_code_disable_admin_env_union` | bool | `null` | `CLAUDE_CODE_DISABLE_ADMIN_ENV_UNION` | Set to 1 to stop Claude Code from merging managed settings env blocks per key across admin sources, so only the highe... |
| `claude.env.claude_code_disable_advisor_tool` | bool | `null` | `CLAUDE_CODE_DISABLE_ADVISOR_TOOL` | Set to 1 to disable the advisor tool |
| `claude.env.claude_code_disable_agent_view` | bool | `null` | `CLAUDE_CODE_DISABLE_AGENT_VIEW` | Set to 1 to turn off background agents and agent view: claude agents, --bg, /background, and the on-demand supervisor |
| `claude.env.claude_code_disable_alternate_screen` | bool | `null` | `CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN` | Set to 1 to disable fullscreen rendering and use the classic main-screen renderer |
| `claude.env.claude_code_disable_artifact` | bool | `null` | `CLAUDE_CODE_DISABLE_ARTIFACT` | Set to 1 to turn off the Artifact tool, which publishes session output as a private web page on claude.ai |
| `claude.env.claude_code_disable_attachments` | bool | `null` | `CLAUDE_CODE_DISABLE_ATTACHMENTS` | Set to 1 to disable attachment processing |
| `claude.env.claude_code_disable_auth_refresh_lock` | bool | `null` | `CLAUDE_CODE_DISABLE_AUTH_REFRESH_LOCK` | Set to 1 to make a Claude Code process run its gcpAuthRefresh or awsAuthRefresh command itself instead of waiting whi... |
| `claude.env.claude_code_disable_auto_memory` | bool | `null` | `CLAUDE_CODE_DISABLE_AUTO_MEMORY` | Set to 1 to disable auto memory |
| `claude.env.claude_code_disable_background_tasks` | bool | `null` | `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS` | Set to 1 to disable all background task functionality, including the run_in_background parameter on Bash and subagent... |
| `claude.env.claude_code_disable_bedrock_content_type_default` | bool | `null` | `CLAUDE_CODE_DISABLE_BEDROCK_CONTENT_TYPE_DEFAULT` | Set to 1 to stop Claude Code from treating an Amazon Bedrock streaming response with a missing or empty Content-Type ... |
| `claude.env.claude_code_disable_bedrock_content_type_guard` | bool | `null` | `CLAUDE_CODE_DISABLE_BEDROCK_CONTENT_TYPE_GUARD` | Set to 1 to skip the check that an Amazon Bedrock streaming response carries the application/vnd.amazon.eventstream c... |
| `claude.env.claude_code_disable_bg_exit_handoff` | bool | `null` | `CLAUDE_CODE_DISABLE_BG_EXIT_HANDOFF` | Set to 1 to stop a background session's running background shell commands, dynamic workflows, and, as of v2.1.198, ba... |
| `claude.env.claude_code_disable_bg_shell_pressure_reap` | bool | `null` | `CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP` | Set to 1 to stop Claude Code from terminating background shell commands under memory pressure |
| `claude.env.claude_code_disable_bundled_skills` | bool | `null` | `CLAUDE_CODE_DISABLE_BUNDLED_SKILLS` | Set to 1 to disable the skills and workflows included with Claude Code: bundled skills and workflows are removed enti... |
| `claude.env.claude_code_disable_cfc_prompt` | bool | `null` | `CLAUDE_CODE_DISABLE_CFC_PROMPT` | Set to 1 to keep the Claude in Chrome browser tools available while omitting the Chrome section of the system prompt ... |
| `claude.env.claude_code_disable_claude_mds` | bool | `null` | `CLAUDE_CODE_DISABLE_CLAUDE_MDS` | Set to 1 to prevent loading any CLAUDE.md memory files into context, including user, project, and auto memory files |
| `claude.env.claude_code_disable_cron` | bool | `null` | `CLAUDE_CODE_DISABLE_CRON` | Set to 1 to disable session-scoped scheduled tasks |
| `claude.env.claude_code_disable_dangerous_rm_timeout` | bool | `null` | `CLAUDE_CODE_DISABLE_DANGEROUS_RM_TIMEOUT` | Set to 1 to turn off the time limit on critical-path removal prompts |
| `claude.env.claude_code_disable_experimental_betas` | bool | `null` | `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` | Set to 1 to strip pre-release anthropic-beta request headers, the body fields that pair with them, and beta tool-sche... |
| `claude.env.claude_code_disable_explore_plan_agents` | bool | `null` | `CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS` | Set to 1 to disable the built-in Explore and Plan subagents |
| `claude.env.claude_code_disable_fast_mode` | bool | `null` | `CLAUDE_CODE_DISABLE_FAST_MODE` | Set to 1 to disable fast mode |
| `claude.env.claude_code_disable_feedback_survey` | bool | `null` | `CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY` | Set to 1 to disable the "How is Claude doing?" session quality surveys |
| `claude.env.claude_code_disable_file_checkpointing` | bool | `null` | `CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING` | Set to 1 to disable file checkpointing |
| `claude.env.claude_code_disable_file_mentions` | bool | `null` | `CLAUDE_CODE_DISABLE_FILE_MENTIONS` | Set to 1 to stop Claude Code from expanding @ mentions of files and directories |
| `claude.env.claude_code_disable_git_instructions` | bool | `null` | `CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS` | Set to 1 to remove built-in commit and PR workflow instructions and the git status snapshot from Claude's context |
| `claude.env.claude_code_disable_inline_shell_rm_prompt` | bool | `null` | `CLAUDE_CODE_DISABLE_INLINE_SHELL_RM_PROMPT` | Set to 1 to stop Claude Code from reading scripts passed to a shell with -c, such as bash -c 'rm -rf ~', for critical... |
| `claude.env.claude_code_disable_legacy_model_remap` | bool | `null` | `CLAUDE_CODE_DISABLE_LEGACY_MODEL_REMAP` | Set to 1 to prevent automatic remapping of Opus 4 and Opus 4.1 to the current Opus version on the Anthropic API. Use ... |
| `claude.env.claude_code_disable_model_access_fallback` | bool | `null` | `CLAUDE_CODE_DISABLE_MODEL_ACCESS_FALLBACK` | Set to 1 to stop Claude Code on Amazon Bedrock and Google Cloud's Agent Platform from switching to an older model whe... |
| `claude.env.claude_code_disable_mounted_drive_rm_prompt` | bool | `null` | `CLAUDE_CODE_DISABLE_MOUNTED_DRIVE_RM_PROMPT` | Set to 1 to turn off the mounted drive prompt for paths such as /Volumes/USB. Set it in the environment that launches... |
| `claude.env.claude_code_disable_mouse` | bool | `null` | `CLAUDE_CODE_DISABLE_MOUSE` | Set to 1 to disable mouse tracking in fullscreen rendering |
| `claude.env.claude_code_disable_mouse_clicks` | bool | `null` | `CLAUDE_CODE_DISABLE_MOUSE_CLICKS` | Set to 1 to disable click, drag, and hover handling in fullscreen rendering while keeping mouse-wheel scrolling |
| `claude.env.claude_code_disable_mtls_reload_on_stale_connection` | bool | `null` | `CLAUDE_CODE_DISABLE_MTLS_RELOAD_ON_STALE_CONNECTION` | Set to 1 to stop Claude Code from re-reading the mTLS client certificate and key when an API request fails with a con... |
| `claude.env.claude_code_disable_nonessential_traffic` | bool | `null` | `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | Set to any non-empty value, such as 1, to disable nonessential network traffic: auto-updates, telemetry, error report... |
| `claude.env.claude_code_disable_nonstreaming_fallback` | bool | `null` | `CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK` | Set to 1 to disable the non-streaming fallback when a streaming request fails mid-stream |
| `claude.env.claude_code_disable_notification_presence_check` | bool | `null` | `CLAUDE_CODE_DISABLE_NOTIFICATION_PRESENCE_CHECK` | Set to 1 to send the PushNotification tool's desktop notification even while you are typing in or focused on the term... |
| `claude.env.claude_code_disable_official_marketplace_autoinstall` | bool | `null` | `CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL` | Set to 1 to disable automatic registration of the official plugin marketplace |
| `claude.env.claude_code_disable_permission_prompt_notify_hooks` | bool | `null` | `CLAUDE_CODE_DISABLE_PERMISSION_PROMPT_NOTIFY_HOOKS` | Set to 1 to stop Claude Code from running your Notification hooks for unanswered permission requests in sessions wher... |
| `claude.env.claude_code_disable_policy_skills` | bool | `null` | `CLAUDE_CODE_DISABLE_POLICY_SKILLS` | Set to 1 to skip loading skills from the system-wide managed skills directory |
| `claude.env.claude_code_disable_powershell_cmd_rm_deny` | bool | `null` | `CLAUDE_CODE_DISABLE_POWERSHELL_CMD_RM_DENY` | Set to 1 to turn off the PowerShell tool check that denies the cmd built-ins rd, rmdir, del, and erase on a system pa... |
| `claude.env.claude_code_disable_refusal_fallback` | bool | `null` | `CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK` | Set to 1 to turn off the automatic model switch when a safety classifier flags a request, the behavior the switchMode... |
| `claude.env.claude_code_disable_structured_outputs` | bool | `null` | `CLAUDE_CODE_DISABLE_STRUCTURED_OUTPUTS` | Set to 1 to stop Claude Code from sending the structured-output output_config.format field and the anthropic-beta val... |
| `claude.env.claude_code_disable_substitution_rm_prompt` | bool | `null` | `CLAUDE_CODE_DISABLE_SUBSTITUTION_RM_PROMPT` | Set to 1 to turn off the critical-path check for a recursive rm whose target is entirely the output of a command subs... |
| `claude.env.claude_code_disable_terminal_title` | bool | `null` | `CLAUDE_CODE_DISABLE_TERMINAL_TITLE` | Set to 1 to disable automatic terminal title updates based on conversation context |
| `claude.env.claude_code_disable_thinking` | bool | `null` | `CLAUDE_CODE_DISABLE_THINKING` | Set to 1 to omit the thinking parameter from API requests entirely |
| `claude.env.claude_code_disable_unknown_model_window_enforcement` | bool | `null` | `CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT` | Set to 1 to skip proactive auto-compaction when Claude Code doesn't recognize the model ID, such as an LLM gateway alias |
| `claude.env.claude_code_disable_virtual_scroll` | bool | `null` | `CLAUDE_CODE_DISABLE_VIRTUAL_SCROLL` | Set to 1 to disable virtual scrolling in fullscreen rendering and render every message in the transcript |
| `claude.env.claude_code_disable_web_fetch` | bool | `null` | `CLAUDE_CODE_DISABLE_WEB_FETCH` | Set to 1 to turn off the WebFetch tool |
| `claude.env.claude_code_disable_windows_shell_launcher` | bool | `null` | `CLAUDE_CODE_DISABLE_WINDOWS_SHELL_LAUNCHER` | Set to 1 to start PowerShell tool commands on Windows directly instead of through the cmd.exe launcher |
| `claude.env.claude_code_disable_workflows` | bool | `null` | `CLAUDE_CODE_DISABLE_WORKFLOWS` | Set to 1 to disable workflows |
| `claude.env.claude_code_effort_level` | str | `null` | `CLAUDE_CODE_EFFORT_LEVEL` | Set the effort level for supported models |
| `claude.env.claude_code_emit_session_state_events` | bool | `null` | `CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS` | Set to 1 to add session_state_changed messages, which carry the session's state, to the message stream |
| `claude.env.claude_code_enable_auto_mode` | str | `null` | `CLAUDE_CODE_ENABLE_AUTO_MODE` | Accepted for compatibility with older releases and has no effect |
| `claude.env.claude_code_enable_away_summary` | str | `null` | `CLAUDE_CODE_ENABLE_AWAY_SUMMARY` | Override session recap availability |
| `claude.env.claude_code_enable_background_plugin_refresh` | bool | `null` | `CLAUDE_CODE_ENABLE_BACKGROUND_PLUGIN_REFRESH` | Set to 1 to refresh plugin state at turn boundaries in non-interactive mode after a background install completes |
| `claude.env.claude_code_enable_cfc` | bool | `null` | `CLAUDE_CODE_ENABLE_CFC` | Set to 1 to start a CLI session with Chrome integration on, or 0 to start with it off |
| `claude.env.claude_code_enable_feedback_survey_for_otel` | bool | `null` | `CLAUDE_CODE_ENABLE_FEEDBACK_SURVEY_FOR_OTEL` | Set to 1 to route the "How is Claude doing?" session quality survey to your own OpenTelemetry collector when Anthropi... |
| `claude.env.claude_code_enable_fine_grained_tool_streaming` | bool | `null` | `CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING` | Controls whether tool call inputs stream from the API as Claude generates them |
| `claude.env.claude_code_enable_gateway_model_discovery` | bool | `null` | `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY` | Set to 1 to populate the /model picker from your gateway's /v1/models endpoint when ANTHROPIC_BASE_URL points at an A... |
| `claude.env.claude_code_enable_opus_4_7_fast_mode` | str | `null` | `CLAUDE_CODE_ENABLE_OPUS_4_7_FAST_MODE` | Removed in v2.1.142, when the fast mode default moved from Opus 4.6 to Opus 4.7 |
| `claude.env.claude_code_enable_prompt_suggestion` | bool | `null` | `CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION` | Set to false to turn off prompt suggestions, the grayed-out predictions that appear in your prompt input |
| `claude.env.claude_code_enable_tasks` | str | `null` | `CLAUDE_CODE_ENABLE_TASKS` | Selects which task-tracking tools Claude Code provides in sessions that have them |
| `claude.env.claude_code_enable_telemetry` | bool | `null` | `CLAUDE_CODE_ENABLE_TELEMETRY` | Set to 1 to enable OpenTelemetry data collection for metrics and logging |
| `claude.env.claude_code_enable_todo_tools` | bool | `null` | `CLAUDE_CODE_ENABLE_TODO_TOOLS` | Set to 1 to get the task-tracking tools on every model |
| `claude.env.claude_code_exit_after_stop_delay` | int | `null` | `CLAUDE_CODE_EXIT_AFTER_STOP_DELAY` | Time in milliseconds to wait after the query loop becomes idle before automatically exiting |
| `claude.env.claude_code_experimental_agent_teams` | bool | `null` | `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` | Set to 1 to enable agent teams |
| `claude.env.claude_code_extra_body` | str | `null` | `CLAUDE_CODE_EXTRA_BODY` | JSON object to merge into the top level of every API request body |
| `claude.env.claude_code_file_read_max_output_tokens` | int | `null` | `CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS` | Override the default token limit for file reads, which is 25,000 tokens |
| `claude.env.claude_code_force_session_persistence` | bool | `null` | `CLAUDE_CODE_FORCE_SESSION_PERSISTENCE` | Set to 1 to force transcript persistence, prompt history, and claude agents registration even when this claude was la... |
| `claude.env.claude_code_force_strikethrough` | bool | `null` | `CLAUDE_CODE_FORCE_STRIKETHROUGH` | Set to 1 to force strikethrough rendering for ~~text~~ in Claude's responses when your terminal supports it but is no... |
| `claude.env.claude_code_force_sync_output` | bool | `null` | `CLAUDE_CODE_FORCE_SYNC_OUTPUT` | Set to 1 to force-enable DEC private mode 2026 synchronized output when your terminal supports it but is not auto-det... |
| `claude.env.claude_code_force_terminal_images` | bool | `null` | `CLAUDE_CODE_FORCE_TERMINAL_IMAGES` | Set to 1 to draw mod Image elements as pictures when your terminal draws kitty graphics protocol images with Unicode ... |
| `claude.env.claude_code_fork_subagent` | str | `null` | `CLAUDE_CODE_FORK_SUBAGENT` | Controls fork mode, which is on by default and lets Claude start forks on its own |
| `claude.env.claude_code_forward_subagent_text` | bool | `null` | `CLAUDE_CODE_FORWARD_SUBAGENT_TEXT` | Set to 1 to emit subagent text and thinking blocks in claude -p --output-format stream-json output, the same behavior... |
| `claude.env.claude_code_gateway_hint_headers` | bool | `null` | `CLAUDE_CODE_GATEWAY_HINT_HEADERS` | Set to 1 to send the gateway hint headers, such as x-claude-code-request-class and x-claude-code-compaction, on a cus... |
| `claude.env.claude_code_gateway_model_discovery_timeout_ms` | int | `null` | `CLAUDE_CODE_GATEWAY_MODEL_DISCOVERY_TIMEOUT_MS` | Timeout in milliseconds for the gateway model discovery request that CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY turns... |
| `claude.env.claude_code_git_bash_path` | str | `null` | `CLAUDE_CODE_GIT_BASH_PATH` | Windows only: path to the Git Bash executable (bash.exe) |
| `claude.env.claude_code_glob_hidden` | str | `null` | `CLAUDE_CODE_GLOB_HIDDEN` | Set to false to exclude dotfiles from results when Claude invokes the Glob tool |
| `claude.env.claude_code_glob_no_ignore` | str | `null` | `CLAUDE_CODE_GLOB_NO_IGNORE` | Set to false to make the Glob tool respect .gitignore patterns |
| `claude.env.claude_code_glob_timeout_seconds` | int | `null` | `CLAUDE_CODE_GLOB_TIMEOUT_SECONDS` | Timeout in seconds for Glob tool file discovery |
| `claude.env.claude_code_goal_checkin_minutes` | int | `null` | `CLAUDE_CODE_GOAL_CHECKIN_MINUTES` | How many minutes background work can keep an active goal waiting before Claude Code asks Claude to check on it |
| `claude.env.claude_code_gzip_request_bodies` | str | `null` | `CLAUDE_CODE_GZIP_REQUEST_BODIES` | Set to 0 to turn off gzip compression of Claude API, telemetry, and artifact publish request bodies sent to api.anthr... |
| `claude.env.claude_code_hide_cwd` | bool | `null` | `CLAUDE_CODE_HIDE_CWD` | Set to 1 to hide the working directory in the startup logo |
| `claude.env.claude_code_ide_host_override` | str | `null` | `CLAUDE_CODE_IDE_HOST_OVERRIDE` | Override the host address used to connect to the IDE extension |
| `claude.env.claude_code_ide_skip_auto_install` | bool | `null` | `CLAUDE_CODE_IDE_SKIP_AUTO_INSTALL` | Set to 1 to skip auto-installation of IDE extensions |
| `claude.env.claude_code_ide_skip_valid_check` | bool | `null` | `CLAUDE_CODE_IDE_SKIP_VALID_CHECK` | Set to 1 to skip validation of IDE lockfile entries during connection |
| `claude.env.claude_code_idle_compact_min_tokens` | int | `null` | `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS` | Context size, in tokens, at which idle compaction applies to a conversation (default: 200000) |
| `claude.env.claude_code_max_concurrent_subagents` | int | `null` | `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` | How many subagents can be running in one session before the Agent tool refuses to spawn another (default: 20) |
| `claude.env.claude_code_max_context_tokens` | int | `null` | `CLAUDE_CODE_MAX_CONTEXT_TOKENS` | Override the context window size Claude Code assumes for the active model |
| `claude.env.claude_code_max_mcp_description_length` | int | `null` | `CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH` | Maximum length in characters of MCP tool descriptions and MCP server instructions that Claude Code sends to the model |
| `claude.env.claude_code_max_output_tokens` | int | `null` | `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | Set the maximum number of output tokens for most requests |
| `claude.env.claude_code_max_retries` | int | `null` | `CLAUDE_CODE_MAX_RETRIES` | Override the number of times to retry failed API requests (default: 10) |
| `claude.env.claude_code_max_subagents_per_session` | str | `null` | `CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION` | Removed in v2.1.224 and now a no-op |
| `claude.env.claude_code_max_subagent_spawn_depth` | int | `null` | `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` | Number of subagent layers allowed below the main conversation (default: 3) |
| `claude.env.claude_code_max_tool_use_concurrency` | int | `null` | `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY` | Maximum number of read-only tools and subagents that can execute in parallel (default: 10) |
| `claude.env.claude_code_max_turns` | int | `null` | `CLAUDE_CODE_MAX_TURNS` | Cap the number of agentic turns when no explicit limit is passed |
| `claude.env.claude_code_max_web_searches_per_session` | int | `null` | `CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION` | Cap on WebSearch calls (default: 200) |
| `claude.env.claude_code_mcp_allowlist_env` | bool | `null` | `CLAUDE_CODE_MCP_ALLOWLIST_ENV` | Set to 1 to spawn stdio MCP servers with only a safe baseline environment plus the server's configured env, instead o... |
| `claude.env.claude_code_mcp_auto_background_ms` | int | `null` | `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS` | Elapsed time in milliseconds before a still-running MCP tool call moves to a background task (default: 120000, or 2 m... |
| `claude.env.claude_code_mcp_startup_wait_ms` | int | `null` | `CLAUDE_CODE_MCP_STARTUP_WAIT_MS` | How long in milliseconds the first turn of a non-interactive session waits for MCP servers that are still connecting,... |
| `claude.env.claude_code_mcp_tool_idle_timeout` | int | `null` | `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` | Idle timeout in milliseconds for MCP tool calls |
| `claude.env.claude_code_messaging_socket` | str | `null` | `CLAUDE_CODE_MESSAGING_SOCKET` | Set by Claude Code, not by you: in sessions that bind an inbox socket, Claude Code exports that socket's path to hook... |
| `claude.env.claude_code_messaging_token` | secret | `null` | `CLAUDE_CODE_MESSAGING_TOKEN` | Set by Claude Code, not by you: in sessions that bind an inbox socket, Claude Code exports this per-session token to ... |
| `claude.env.claude_code_native_cursor` | bool | `null` | `CLAUDE_CODE_NATIVE_CURSOR` | Set to 1 to show the terminal's own cursor at the input caret instead of a drawn block |
| `claude.env.claude_code_new_init` | bool | `null` | `CLAUDE_CODE_NEW_INIT` | Set to 1 to make /init run an interactive setup flow |
| `claude.env.claude_code_nonblocking_stdout` | bool | `null` | `CLAUDE_CODE_NONBLOCKING_STDOUT` | Set to 1 to write terminal output through a second non-blocking file descriptor, so a terminal that stops reading, su... |
| `claude.env.claude_code_nonstreaming_timeout_retries` | int | `null` | `CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES` | Limit how many times Claude Code re-sends a non-streaming request that times out |
| `claude.env.claude_code_no_flicker` | bool | `null` | `CLAUDE_CODE_NO_FLICKER` | Set to 1 to enable fullscreen rendering, a research preview that reduces flicker and keeps memory flat in long conver... |
| `claude.env.claude_code_oauth_refresh_token` | secret | `null` | `CLAUDE_CODE_OAUTH_REFRESH_TOKEN` | OAuth refresh token for Claude.ai authentication |
| `claude.env.claude_code_oauth_scopes` | str | `null` | `CLAUDE_CODE_OAUTH_SCOPES` | Space-separated OAuth scopes the refresh token was issued with, such as "user:profile user:inference user:sessions:cl... |
| `claude.env.claude_code_oauth_token` | secret | `null` | `CLAUDE_CODE_OAUTH_TOKEN` | OAuth access token for claude.ai authentication |
| `claude.env.claude_code_opus_4_6_fast_mode_override` | str | `null` | `CLAUDE_CODE_OPUS_4_6_FAST_MODE_OVERRIDE` | Removed in v2.1.160 and now a no-op |
| `claude.env.claude_code_otel_content_max_length` | int | `null` | `CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH` | Maximum length of content-bearing OpenTelemetry attributes (model responses, tool content, system prompts, raw API bo... |
| `claude.env.claude_code_otel_diag_stderr` | bool | `null` | `CLAUDE_CODE_OTEL_DIAG_STDERR` | Set to 1 to write OpenTelemetry exporter diagnostic errors to stderr |
| `claude.env.claude_code_otel_flush_timeout_ms` | int | `null` | `CLAUDE_CODE_OTEL_FLUSH_TIMEOUT_MS` | Timeout in milliseconds for flushing pending OpenTelemetry spans (default: 5000) |
| `claude.env.claude_code_otel_headers_helper_debounce_ms` | int | `null` | `CLAUDE_CODE_OTEL_HEADERS_HELPER_DEBOUNCE_MS` | Interval for refreshing dynamic OpenTelemetry headers in milliseconds (default: 1740000 / 29 minutes) |
| `claude.env.claude_code_otel_shutdown_timeout_ms` | int | `null` | `CLAUDE_CODE_OTEL_SHUTDOWN_TIMEOUT_MS` | Timeout in milliseconds for the OpenTelemetry exporter to finish on shutdown (default: 2000) |
| `claude.env.claude_code_overloaded_retry_base_delay_ms` | int | `null` | `CLAUDE_CODE_OVERLOADED_RETRY_BASE_DELAY_MS` | Starting delay in milliseconds, in place of the default 500, of the exponential backoff between automatic retries of ... |
| `claude.env.claude_code_overloaded_retry_max_delay_ms` | int | `null` | `CLAUDE_CODE_OVERLOADED_RETRY_MAX_DELAY_MS` | Ceiling in milliseconds, in place of the default 32000, on the exponential backoff between automatic retries of a req... |
| `claude.env.claude_code_package_manager_auto_update` | bool | `null` | `CLAUDE_CODE_PACKAGE_MANAGER_AUTO_UPDATE` | Set to 1 to let Claude Code run your package manager's upgrade command in the background when a new version is available |
| `claude.env.claude_code_perforce_mode` | bool | `null` | `CLAUDE_CODE_PERFORCE_MODE` | Set to 1 to enable Perforce-aware write protection |
| `claude.env.claude_code_plugin_cache_dir` | str | `null` | `CLAUDE_CODE_PLUGIN_CACHE_DIR` | Override the plugins root directory |
| `claude.env.claude_code_plugin_dirs` | str | `null` | `CLAUDE_CODE_PLUGIN_DIRS` | Plugin directories to load for the session, each loaded the way a --plugin-dir flag loads it |
| `claude.env.claude_code_plugin_dir_watch` | bool | `null` | `CLAUDE_CODE_PLUGIN_DIR_WATCH` | Controls whether Claude Code reloads a mod when the mod's files change |
| `claude.env.claude_code_plugin_git_timeout_ms` | int | `null` | `CLAUDE_CODE_PLUGIN_GIT_TIMEOUT_MS` | Timeout in milliseconds for cloning or refreshing a plugin marketplace (default: 120000) |
| `claude.env.claude_code_plugin_keep_marketplace_on_failure` | bool | `null` | `CLAUDE_CODE_PLUGIN_KEEP_MARKETPLACE_ON_FAILURE` | Set to 1 to skip the re-clone attempt and keep using the existing marketplace checkout when a marketplace refresh can... |
| `claude.env.claude_code_plugin_prefer_https` | bool | `null` | `CLAUDE_CODE_PLUGIN_PREFER_HTTPS` | Set to 1 to clone GitHub owner/repo shorthand sources over HTTPS instead of SSH. Applies to plugin install and update... |
| `claude.env.claude_code_plugin_seed_dir` | str | `null` | `CLAUDE_CODE_PLUGIN_SEED_DIR` | Path to one or more read-only plugin seed directories, separated by : on Unix or ; on Windows |
| `claude.env.claude_code_powershell_respect_execution_policy` | bool | `null` | `CLAUDE_CODE_POWERSHELL_RESPECT_EXECUTION_POLICY` | Set to 1 to stop Claude Code from passing -ExecutionPolicy Bypass when spawning PowerShell for tool calls, hooks, and... |
| `claude.env.claude_code_print_bg_wait_ceiling_ms` | int | `null` | `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` | Ceiling in milliseconds on idle waiting for background work, such as subagents and workflows, after the final turn in... |
| `claude.env.claude_code_process_wrapper` | str | `null` | `CLAUDE_CODE_PROCESS_WRAPPER` | Launch the processes Claude Code starts from its own binary, such as the background service that hosts agent view ses... |
| `claude.env.claude_code_project_dir_name` | str | `null` | `CLAUDE_CODE_PROJECT_DIR_NAME` | Set together with CLAUDE_CONFIG_DIR to choose the projects/ directory name Claude Code stores that session's transcri... |
| `claude.env.claude_code_prompt_cache_ttl` | str | `null` | `CLAUDE_CODE_PROMPT_CACHE_TTL` | Set 5m or 1h, the only values Claude Code accepts, to choose the prompt cache TTL for the main conversation: your int... |
| `claude.env.claude_code_propagate_traceparent` | bool | `null` | `CLAUDE_CODE_PROPAGATE_TRACEPARENT` | Set to 1 to propagate W3C trace context when ANTHROPIC_BASE_URL points at a custom proxy |
| `claude.env.claude_code_provider_managed_by_host` | str | `null` | `CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST` | Set by host platforms that embed Claude Code and manage model provider routing on its behalf |
| `claude.env.claude_code_proxy_resolves_hosts` | bool | `null` | `CLAUDE_CODE_PROXY_RESOLVES_HOSTS` | Set to 1 to allow the proxy to perform DNS resolution instead of the caller |
| `claude.env.claude_code_remote` | str | `null` | `CLAUDE_CODE_REMOTE` | Set automatically to true when Claude Code is running as a cloud session |
| `claude.env.claude_code_remote_session_id` | str | `null` | `CLAUDE_CODE_REMOTE_SESSION_ID` | Set automatically in cloud sessions to the current session's ID. Read this to construct a link back to the session tr... |
| `claude.env.claude_code_restricted` | bool | `null` | `CLAUDE_CODE_RESTRICTED` | Set to 1 to start the session in restricted mode, the same as passing --restricted |
| `claude.env.claude_code_resume_interrupted_turn` | bool | `null` | `CLAUDE_CODE_RESUME_INTERRUPTED_TURN` | Set to 1 to automatically resume if the previous session ended mid-turn |
| `claude.env.claude_code_resume_interrupted_turn_max_age_ms` | int | `null` | `CLAUDE_CODE_RESUME_INTERRUPTED_TURN_MAX_AGE_MS` | Maximum age in milliseconds of the last transcript message for a session that ended mid-turn to continue automaticall... |
| `claude.env.claude_code_resume_prompt` | str | `null` | `CLAUDE_CODE_RESUME_PROMPT` | Override the continuation message Claude Code sends to Claude when CLAUDE_CODE_RESUME_INTERRUPTED_TURN continues an i... |
| `claude.env.claude_code_retry_watchdog` | bool | `null` | `CLAUDE_CODE_RETRY_WATCHDOG` | Set to 1 for unattended sessions such as eval harnesses, CI jobs, or remote workers |
| `claude.env.claude_code_retry_watchdog_max_wait_ms` | int | `null` | `CLAUDE_CODE_RETRY_WATCHDOG_MAX_WAIT_MS` | Maximum time in milliseconds that each API request spends waiting out 429 and 529 errors when CLAUDE_CODE_RETRY_WATCH... |
| `claude.env.claude_code_safe_mode` | bool | `null` | `CLAUDE_CODE_SAFE_MODE` | Set to 1 to start in safe mode: CLAUDE.md, skills, plugins, hooks, MCP servers, custom commands and agents, output st... |
| `claude.env.claude_code_script_caps` | str | `null` | `CLAUDE_CODE_SCRIPT_CAPS` | JSON object limiting how many times specific scripts may be invoked per session when CLAUDE_CODE_SUBPROCESS_ENV_SCRUB... |
| `claude.env.claude_code_scroll_speed` | str | `null` | `CLAUDE_CODE_SCROLL_SPEED` | Set the mouse wheel scroll multiplier in fullscreen rendering |
| `claude.env.claude_code_send_feedback` | bool | `null` | `CLAUDE_CODE_SEND_FEEDBACK` | Set to 0 to turn off Claude-drafted feedback for a session |
| `claude.env.claude_code_sessionend_hooks_timeout_ms` | int | `null` | `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` | Override the time budget in milliseconds for SessionEnd hooks |
| `claude.env.claude_code_session_id` | str | `null` | `CLAUDE_CODE_SESSION_ID` | Set automatically to the current session ID in Bash and PowerShell tool subprocesses, hook command subprocesses, and ... |
| `claude.env.claude_code_shell` | str | `null` | `CLAUDE_CODE_SHELL` | Set the shell Claude Code uses to run Bash tool commands |
| `claude.env.claude_code_shell_prefix` | str | `null` | `CLAUDE_CODE_SHELL_PREFIX` | Command prefix that wraps shell commands Claude Code spawns: Bash tool calls, hook commands, status line commands, an... |
| `claude.env.claude_code_simple` | bool | `null` | `CLAUDE_CODE_SIMPLE` | Set to 1 to run with a minimal system prompt and only the Bash, file read, and file edit tools |
| `claude.env.claude_code_simple_system_prompt` | bool | `null` | `CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT` | Choose between Claude Code's full system prompt and a shorter one with abbreviated tool descriptions |
| `claude.env.claude_code_skip_anthropic_aws_auth` | bool | `null` | `CLAUDE_CODE_SKIP_ANTHROPIC_AWS_AUTH` | Skip client-side authentication for Claude Platform on AWS, for gateways that sign requests themselves |
| `claude.env.claude_code_skip_aws_cred_cache` | bool | `null` | `CLAUDE_CODE_SKIP_AWS_CRED_CACHE` | Set to 1 to turn off the in-process cache of credentials resolved from the AWS default credential provider chain, so ... |
| `claude.env.claude_code_skip_bedrock_auth` | bool | `null` | `CLAUDE_CODE_SKIP_BEDROCK_AUTH` | Skip AWS authentication for Amazon Bedrock (for example, when using an LLM gateway) |
| `claude.env.claude_code_skip_fast_mode_network_errors` | bool | `null` | `CLAUDE_CODE_SKIP_FAST_MODE_NETWORK_ERRORS` | Set to 1 to treat a failed fast mode availability check as available, for networks that block the check's direct requ... |
| `claude.env.claude_code_skip_fast_mode_org_check` | bool | `null` | `CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK` | Set to 1 to skip the client-side fast mode availability check, for proxies that intercept the check's request rather ... |
| `claude.env.claude_code_skip_foundry_auth` | bool | `null` | `CLAUDE_CODE_SKIP_FOUNDRY_AUTH` | Skip Azure authentication for Microsoft Foundry, for a proxy or gateway that injects its own Authorization header |
| `claude.env.claude_code_skip_mantle_auth` | bool | `null` | `CLAUDE_CODE_SKIP_MANTLE_AUTH` | Skip AWS authentication for Amazon Bedrock Mantle (for example, when using an LLM gateway) |
| `claude.env.claude_code_skip_model_access_memory` | str | `null` | `CLAUDE_CODE_SKIP_MODEL_ACCESS_MEMORY` | The startup model checks on Amazon Bedrock and Google Cloud's Agent Platform remember on this machine which models th... |
| `claude.env.claude_code_skip_prompt_history` | bool | `null` | `CLAUDE_CODE_SKIP_PROMPT_HISTORY` | Set to 1 to skip writing prompt history and session transcripts to disk |
| `claude.env.claude_code_skip_vertex_auth` | bool | `null` | `CLAUDE_CODE_SKIP_VERTEX_AUTH` | Skip Google authentication for Google Cloud's Agent Platform (for example, when using an LLM gateway) |
| `claude.env.claude_code_startup_failure_results` | bool | `null` | `CLAUDE_CODE_STARTUP_FAILURE_RESULTS` | Set to 1 to have a session started with --output-format stream-json write a result message naming why Claude Code ref... |
| `claude.env.claude_code_stop_hook_block_cap` | str | `null` | `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` | Maximum number of consecutive times a Stop or SubagentStop hook may block the turn from ending before Claude Code ove... |
| `claude.env.claude_code_subagent_model` | str | `null` | `CLAUDE_CODE_SUBAGENT_MODEL` | The default model for subagents, agent team teammates, and workflow agents that aren't assigned a model another way |
| `claude.env.claude_code_subagent_model_force` | bool | `null` | `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` | Set to 1 to force one model onto subagents, teammates, and workflow agents |
| `claude.env.claude_code_subagent_prompt_cache_ttl` | str | `null` | `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL` | Set 5m or 1h, the only values Claude Code accepts, to choose the prompt cache TTL for requests outside the main conve... |
| `claude.env.claude_code_subprocess_env_scrub` | bool | `null` | `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` | Set to 1 to strip credentials from the environments of the subprocesses Claude Code starts, such as Bash commands, ho... |
| `claude.env.claude_code_sync_plugin_install` | bool | `null` | `CLAUDE_CODE_SYNC_PLUGIN_INSTALL` | Set to 1 in non-interactive mode (the -p flag) to wait for plugin installation to complete before the first query |
| `claude.env.claude_code_sync_plugin_install_timeout_ms` | int | `null` | `CLAUDE_CODE_SYNC_PLUGIN_INSTALL_TIMEOUT_MS` | Timeout in milliseconds for synchronous plugin installation |
| `claude.env.claude_code_sync_skills` | bool | `null` | `CLAUDE_CODE_SYNC_SKILLS` | Set to 1 in non-interactive mode with the -p flag to make Claude Code download the skills enabled for your claude.ai ... |
| `claude.env.claude_code_sync_skills_install_timeout_ms` | int | `null` | `CLAUDE_CODE_SYNC_SKILLS_INSTALL_TIMEOUT_MS` | Timeout in milliseconds for the skills resync that runs mid-session when an app built on the Agent SDK reloads skills... |
| `claude.env.claude_code_sync_skills_wait_timeout_ms` | int | `null` | `CLAUDE_CODE_SYNC_SKILLS_WAIT_TIMEOUT_MS` | Timeout in milliseconds for the first query to wait for the initial skill list when CLAUDE_CODE_SYNC_SKILLS is set (d... |
| `claude.env.claude_code_syntax_highlight` | str | `null` | `CLAUDE_CODE_SYNTAX_HIGHLIGHT` | Set to false to disable syntax highlighting in diff output |
| `claude.env.claude_code_task_list_id` | str | `null` | `CLAUDE_CODE_TASK_LIST_ID` | Share a task list across sessions |
| `claude.env.claude_code_team_teardown_park_timeout_ms` | int | `null` | `CLAUDE_CODE_TEAM_TEARDOWN_PARK_TIMEOUT_MS` | Override, in milliseconds, how long a non-interactive session waits at exit for its agent team to finish tearing down |
| `claude.env.claude_code_tmpdir` | str | `null` | `CLAUDE_CODE_TMPDIR` | Override the temp directory used for internal temp files |
| `claude.env.claude_code_tmux_truecolor` | str | `null` | `CLAUDE_CODE_TMUX_TRUECOLOR` | Set to any non-empty value, such as 1, to allow 24-bit truecolor output inside tmux |
| `claude.env.claude_code_tool_memory_cgroup_exclude` | str | `null` | `CLAUDE_CODE_TOOL_MEMORY_CGROUP_EXCLUDE` | On Linux and WSL, set to a comma-separated list of the kinds of processes Claude Code excludes from the tool memory c... |
| `claude.env.claude_code_tool_memory_limit` | str | `null` | `CLAUDE_CODE_TOOL_MEMORY_LIMIT` | On Linux and WSL, set to a size such as 4G to cap the memory that Bash and PowerShell tool commands can use, and Moni... |
| `claude.env.claude_code_transcript_local_gc` | bool | `null` | `CLAUDE_CODE_TRANSCRIPT_LOCAL_GC` | Set to 1 to limit how large the transcript file of a long -p or Agent SDK session grows |
| `claude.env.claude_code_user_dialog_timeout_ms` | int | `null` | `CLAUDE_CODE_USER_DIALOG_TIMEOUT_MS` | Deadline in milliseconds before Claude Code cancels a dialog it forwards to a remote client such as a Remote Control ... |
| `claude.env.claude_code_use_anthropic_aws` | bool | `null` | `CLAUDE_CODE_USE_ANTHROPIC_AWS` | Use Claude Platform on AWS |
| `claude.env.claude_code_use_bedrock` | bool | `null` | `CLAUDE_CODE_USE_BEDROCK` | Use Amazon Bedrock |
| `claude.env.claude_code_use_foundry` | bool | `null` | `CLAUDE_CODE_USE_FOUNDRY` | Use Microsoft Foundry |
| `claude.env.claude_code_use_mantle` | bool | `null` | `CLAUDE_CODE_USE_MANTLE` | Use the Amazon Bedrock Mantle endpoint |
| `claude.env.claude_code_use_native_file_search` | bool | `null` | `CLAUDE_CODE_USE_NATIVE_FILE_SEARCH` | Set to 1 to discover custom commands, subagents, and output styles using Node.js file APIs instead of ripgrep |
| `claude.env.claude_code_use_powershell_tool` | bool | `null` | `CLAUDE_CODE_USE_POWERSHELL_TOOL` | Controls the PowerShell tool |
| `claude.env.claude_code_use_vertex` | bool | `null` | `CLAUDE_CODE_USE_VERTEX` | Use Google Cloud's Agent Platform |
| `claude.env.claude_code_webfetch_cache_ttl_ms` | int | `null` | `CLAUDE_CODE_WEBFETCH_CACHE_TTL_MS` | Set to the number of milliseconds WebFetch keeps each fetched URL's response cached |
| `claude.env.claude_code_webfetch_deadline_ms` | int | `null` | `CLAUDE_CODE_WEBFETCH_DEADLINE_MS` | Upper bound in milliseconds on how long WebFetch waits for a page to download, including any redirects it follows |
| `claude.env.claude_code_web_search_refills_per_hour` | str | `null` | `CLAUDE_CODE_WEB_SEARCH_REFILLS_PER_HOUR` | Rate at which a session's WebSearch limit refills, in calls per hour |
| `claude.env.claude_code_worker_checkin_schedule` | bool | `null` | `CLAUDE_CODE_WORKER_CHECKIN_SCHEDULE` | When CLAUDE_AUTO_BACKGROUND_TASKS is set to 1, how long Claude Code waits before each reminder to Claude to check on ... |
| `claude.env.claude_code_workflow_max_concurrent_agents` | int | `null` | `CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS` | How many agents a single workflow run executes at once, from 1 to 256. By default, a run executes up to 16 agents at ... |
| `claude.env.claude_code_workflow_prefix_stagger_ms` | int | `null` | `CLAUDE_CODE_WORKFLOW_PREFIX_STAGGER_MS` | Upper bound in milliseconds on how long a workflow agent waits for a same-prefix sibling's first response to begin be... |
| `claude.env.claude_code_you_should_know_debug` | bool | `null` | `CLAUDE_CODE_YOU_SHOULD_KNOW_DEBUG` | Set to 1 to show, above the prompt, a line for each of the last three checks the You should know mod made in this ses... |
| `claude.env.claude_config_dir` | str | `null` | `CLAUDE_CONFIG_DIR` | Override the configuration directory (default: ~/.claude) (managed by the service; cannot be set) |
| `claude.env.claude_disable_adopt` | bool | `null` | `CLAUDE_DISABLE_ADOPT` | Set to 1 to stop in-flight background work instead of carrying it over when you background a session by pressing  or ... |
| `claude.env.claude_effort` | str | `null` | `CLAUDE_EFFORT` | Set automatically in Bash tool subprocesses and hook commands to the effort level in effect when the subprocess start... |
| `claude.env.claude_enable_byte_watchdog` | bool | `null` | `CLAUDE_ENABLE_BYTE_WATCHDOG` | Set to 1 to force-enable the byte-level streaming idle watchdog, or set to 0 to force-disable it |
| `claude.env.claude_enable_byte_watchdog_bedrock` | bool | `null` | `CLAUDE_ENABLE_BYTE_WATCHDOG_BEDROCK` | Set to 1 to enable the byte-level streaming idle watchdog on Amazon Bedrock vnd.amazon.eventstream responses, which a... |
| `claude.env.claude_enable_stream_watchdog` | str | `null` | `CLAUDE_ENABLE_STREAM_WATCHDOG` | Set to 0 to force-disable the event-level streaming idle watchdog, or set to 1 to force-enable it |
| `claude.env.claude_env_file` | str | `null` | `CLAUDE_ENV_FILE` | Path to a shell script whose contents Claude Code runs before each Bash command in the same shell process, so exports... |
| `claude.env.claude_job_dir` | str | `null` | `CLAUDE_JOB_DIR` | Set by Claude Code in each background session to that session's ~/.claude/jobs/<id> directory |
| `claude.env.claude_pid` | str | `null` | `CLAUDE_PID` | Claude Code sets this to its own process ID in the subprocesses it spawns: Bash and PowerShell tool commands and hook... |
| `claude.env.claude_remote_control_session_name_prefix` | str | `null` | `CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX` | Prefix for auto-generated Remote Control session names when no explicit name is provided |
| `claude.env.claude_stream_first_byte_timeout_ms` | int | `null` | `CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS` | Deadline in milliseconds for the first response byte of a streaming request, on the connections where the first-byte ... |
| `claude.env.claude_stream_idle_timeout_ms` | int | `null` | `CLAUDE_STREAM_IDLE_TIMEOUT_MS` | Timeout in milliseconds before the event- and byte-level streaming idle watchdogs close a stalled connection |
| `claude.env.claude_subagent_bg_shell_max_ms` | str | `null` | `CLAUDE_SUBAGENT_BG_SHELL_MAX_MS` | Removed in v2.1.260 and now a no-op |
| `claude.env.cloud_ml_region` | str | `null` | `CLOUD_ML_REGION` | Google Cloud region for Vertex AI |
| `claude.env.debug` | bool | `null` | `DEBUG` | Set to 1 to enable debug mode, equivalent to launching with --debug |
| `claude.env.disable_autoupdater` | bool | `null` | `DISABLE_AUTOUPDATER` | Set to 1 to disable automatic background updates |
| `claude.env.disable_auto_compact` | bool | `null` | `DISABLE_AUTO_COMPACT` | Set to 1 to disable automatic compaction when approaching the context limit, along with idle compaction |
| `claude.env.disable_compact` | bool | `null` | `DISABLE_COMPACT` | Set to 1 to disable all compaction: both automatic compaction and the manual /compact command |
| `claude.env.disable_cost_warnings` | bool | `null` | `DISABLE_COST_WARNINGS` | Set to 1 to disable cost warning messages |
| `claude.env.disable_doctor_command` | bool | `null` | `DISABLE_DOCTOR_COMMAND` | Set to 1 to hide the /doctor setup checkup skill and its /checkup alias |
| `claude.env.disable_error_reporting` | bool | `null` | `DISABLE_ERROR_REPORTING` | Set to any non-empty value, such as 1, to opt out of error reporting |
| `claude.env.disable_extra_usage_command` | bool | `null` | `DISABLE_EXTRA_USAGE_COMMAND` | Set to 1 to hide the /usage-credits command that lets users purchase additional usage beyond rate limits |
| `claude.env.disable_feedback_command` | bool | `null` | `DISABLE_FEEDBACK_COMMAND` | Set to 1 to disable the /feedback command and Claude-drafted feedback |
| `claude.env.disable_growthbook` | bool | `null` | `DISABLE_GROWTHBOOK` | Set to 1 or true to disable GrowthBook feature-flag fetching and use code defaults for every flag |
| `claude.env.disable_installation_checks` | bool | `null` | `DISABLE_INSTALLATION_CHECKS` | Set to 1 to disable installation warnings |
| `claude.env.disable_install_github_app_command` | bool | `null` | `DISABLE_INSTALL_GITHUB_APP_COMMAND` | Set to 1 to hide the /install-github-app command |
| `claude.env.disable_interleaved_thinking` | bool | `null` | `DISABLE_INTERLEAVED_THINKING` | Set to 1 to prevent sending the interleaved-thinking beta header |
| `claude.env.disable_login_command` | bool | `null` | `DISABLE_LOGIN_COMMAND` | Set to 1 to hide the /login command |
| `claude.env.disable_logout_command` | bool | `null` | `DISABLE_LOGOUT_COMMAND` | Set to 1 to hide the /logout command |
| `claude.env.disable_prompt_caching` | bool | `null` | `DISABLE_PROMPT_CACHING` | Set to 1 to disable prompt caching for all models (takes precedence over per-model settings) |
| `claude.env.disable_prompt_caching_fable` | bool | `null` | `DISABLE_PROMPT_CACHING_FABLE` | Set to 1 to disable prompt caching for Fable models |
| `claude.env.disable_prompt_caching_haiku` | bool | `null` | `DISABLE_PROMPT_CACHING_HAIKU` | Set to 1 to disable prompt caching for the default Haiku model, wherever it runs |
| `claude.env.disable_prompt_caching_opus` | bool | `null` | `DISABLE_PROMPT_CACHING_OPUS` | Set to 1 to disable prompt caching for the default Opus model |
| `claude.env.disable_prompt_caching_sonnet` | bool | `null` | `DISABLE_PROMPT_CACHING_SONNET` | Set to 1 to disable prompt caching for the default Sonnet model |
| `claude.env.disable_telemetry` | bool | `null` | `DISABLE_TELEMETRY` | Set to any non-empty value, such as 1, to opt out of telemetry |
| `claude.env.disable_updates` | bool | `null` | `DISABLE_UPDATES` | Set to 1 to block all updates including manual claude update and claude install |
| `claude.env.disable_upgrade_command` | bool | `null` | `DISABLE_UPGRADE_COMMAND` | Set to 1 to hide the /upgrade command |
| `claude.env.do_not_track` | bool | `null` | `DO_NOT_TRACK` | Set to 1 to opt out of telemetry, with the same effect as DISABLE_TELEMETRY, including on feature-flag fetching |
| `claude.env.enable_beta_tracing_detailed` | bool | `null` | `ENABLE_BETA_TRACING_DETAILED` | Set to 1, and set BETA_TRACING_ENDPOINT to your OTLP/HTTP collector endpoint, to turn on detailed beta tracing, which... |
| `claude.env.enable_claudeai_mcp_servers` | str | `null` | `ENABLE_CLAUDEAI_MCP_SERVERS` | Set to false to stop Claude Code from fetching claude.ai MCP servers |
| `claude.env.enable_prompt_caching_1h` | bool | `null` | `ENABLE_PROMPT_CACHING_1H` | Set to 1 to request a 1-hour prompt cache TTL instead of the default 5 minutes |
| `claude.env.enable_prompt_caching_1h_bedrock` | str | `null` | `ENABLE_PROMPT_CACHING_1H_BEDROCK` | Deprecated |
| `claude.env.enable_tool_search` | str | `null` | `ENABLE_TOOL_SEARCH` | Controls MCP tool search |
| `claude.env.fallback_for_all_primary_models` | str | `null` | `FALLBACK_FOR_ALL_PRIMARY_MODELS` | Set to any non-empty value, such as 1, to make Claude Code stop retrying on repeated overload errors for every model ... |
| `claude.env.force_autoupdate_plugins` | bool | `null` | `FORCE_AUTOUPDATE_PLUGINS` | Set to 1 to force plugin auto-updates even when the main auto-updater is disabled via DISABLE_AUTOUPDATER |
| `claude.env.force_hyperlink` | bool | `null` | `FORCE_HYPERLINK` | Set to 1 to enable clickable OSC 8 hyperlinks when your terminal supports them but isn't auto-detected, or 0 to disab... |
| `claude.env.force_prompt_caching_5m` | bool | `null` | `FORCE_PROMPT_CACHING_5M` | Set to 1 to force the 5-minute prompt cache TTL even when 1-hour TTL would otherwise apply |
| `claude.env.https_proxy` | str | `null` | `HTTPS_PROXY` | Specify HTTPS proxy server for network connections |
| `claude.env.http_proxy` | str | `null` | `HTTP_PROXY` | Specify HTTP proxy server for network connections |
| `claude.env.is_demo` | str | `null` | `IS_DEMO` | Set to any non-empty value, such as 1, to enable demo mode: hides your email and organization name from the header an... |
| `claude.env.max_mcp_output_tokens` | int | `null` | `MAX_MCP_OUTPUT_TOKENS` | Maximum number of tokens allowed in MCP tool responses (default: 25000) |
| `claude.env.max_structured_output_retries` | int | `null` | `MAX_STRUCTURED_OUTPUT_RETRIES` | Number of attempts Claude Code allows when the model's response fails validation against the --json-schema in non-int... |
| `claude.env.max_thinking_tokens` | int | `null` | `MAX_THINKING_TOKENS` | Fixed token budget for extended thinking |
| `claude.env.mcp_client_secret` | secret | `null` | `MCP_CLIENT_SECRET` | OAuth client secret for MCP servers that require pre-configured credentials |
| `claude.env.mcp_connection_nonblocking` | str | `null` | `MCP_CONNECTION_NONBLOCKING` | Controls whether startup waits for MCP servers to connect before the first query |
| `claude.env.mcp_connect_timeout_ms` | int | `null` | `MCP_CONNECT_TIMEOUT_MS` | How long blocking MCP startup waits, in milliseconds, for the connection batch before snapshotting the tool list (def... |
| `claude.env.mcp_discovery_cache` | bool | `null` | `MCP_DISCOVERY_CACHE` | Turns the MCP discovery cache on or off |
| `claude.env.mcp_discovery_cache_max_stale_s` | int | `null` | `MCP_DISCOVERY_CACHE_MAX_STALE_S` | Maximum age, in seconds, of a discovery-cache entry (default: 14400, or 4 hours) |
| `claude.env.mcp_discovery_cache_strikes` | str | `null` | `MCP_DISCOVERY_CACHE_STRIKES` | At a start where a discovery-cache entry is older than MCP_DISCOVERY_CACHE_TTL_S, Claude Code refreshes it in the bac... |
| `claude.env.mcp_discovery_cache_ttl_s` | str | `null` | `MCP_DISCOVERY_CACHE_TTL_S` | Seconds for which Claude Code uses a discovery-cache entry without refreshing it (default: 900) |
| `claude.env.mcp_oauth_callback_port` | int | `null` | `MCP_OAUTH_CALLBACK_PORT` | Fixed port for the OAuth redirect callback, as an alternative to --callback-port when adding an MCP server with pre-c... |
| `claude.env.mcp_protocol_negotiation` | str | `null` | `MCP_PROTOCOL_NEGOTIATION` | On the v2 MCP client runtime only, whether Claude Code probes servers for MCP protocol revision 2026-07-28. Set auto ... |
| `claude.env.mcp_remote_server_connection_batch_size` | int | `null` | `MCP_REMOTE_SERVER_CONNECTION_BATCH_SIZE` | Maximum number of remote MCP servers (HTTP/SSE) to connect in parallel during startup (default: 20) |
| `claude.env.mcp_sdk_generation` | str | `null` | `MCP_SDK_GENERATION` | Pin which MCP client runtime this process connects to MCP servers with: v1, built on MCP TypeScript SDK 1.x, or v2, b... |
| `claude.env.mcp_server_connection_batch_size` | int | `null` | `MCP_SERVER_CONNECTION_BATCH_SIZE` | Maximum number of local MCP servers (stdio) to connect in parallel during startup (default: 3) |
| `claude.env.mcp_timeout` | int | `null` | `MCP_TIMEOUT` | Timeout in milliseconds for MCP server startup (default: 30000, or 30 seconds) |
| `claude.env.mcp_tool_timeout` | int | `null` | `MCP_TOOL_TIMEOUT` | Timeout in milliseconds for MCP tool execution (default: 100000000, about 28 hours) |
| `claude.env.no_proxy` | str | `null` | `NO_PROXY` | List of domains and IPs to which requests will be directly issued, bypassing proxy |
| `claude.env.otel_attribute_value_length_limit` | int | `null` | `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT` | Standard OpenTelemetry SDK limit on attribute value length |
| `claude.env.otel_log_assistant_responses` | bool | `null` | `OTEL_LOG_ASSISTANT_RESPONSES` | Set to 1 to include the model's response text on assistant_response OpenTelemetry log events |
| `claude.env.otel_log_managed_settings` | bool | `null` | `OTEL_LOG_MANAGED_SETTINGS` | Set to 1 to add the redacted managed settings, and a SHA-256 digest of the settings before redaction, to managed_sett... |
| `claude.env.otel_log_raw_api_bodies` | bool | `null` | `OTEL_LOG_RAW_API_BODIES` | Emit Anthropic Messages API request and response JSON as api_request_body / api_response_body log events |
| `claude.env.otel_log_tool_content` | bool | `null` | `OTEL_LOG_TOOL_CONTENT` | Set to 1 to include tool content in the tool.output OpenTelemetry span event |
| `claude.env.otel_log_tool_details` | bool | `null` | `OTEL_LOG_TOOL_DETAILS` | Set to 1 to include tool input arguments; MCP server names; user-authored workflow names; raw error strings on tool f... |
| `claude.env.otel_log_user_prompts` | bool | `null` | `OTEL_LOG_USER_PROMPTS` | Set to 1 to include user prompt text in OpenTelemetry traces and logs |
| `claude.env.otel_metrics_include_account_uuid` | str | `null` | `OTEL_METRICS_INCLUDE_ACCOUNT_UUID` | Set to false to exclude account UUID from metrics attributes (default: included) |
| `claude.env.otel_metrics_include_entrypoint` | bool | `null` | `OTEL_METRICS_INCLUDE_ENTRYPOINT` | Set to true to include the session entrypoint in metrics attributes (default: excluded) |
| `claude.env.otel_metrics_include_repository` | bool | `null` | `OTEL_METRICS_INCLUDE_REPOSITORY` | Set to true to tag OpenTelemetry metrics and events with vcs.* attributes identifying the session's repository (defau... |
| `claude.env.otel_metrics_include_resource_attributes` | str | `null` | `OTEL_METRICS_INCLUDE_RESOURCE_ATTRIBUTES` | As of v2.1.161, Claude Code attaches OTEL_RESOURCE_ATTRIBUTES keys to metric datapoint labels |
| `claude.env.otel_metrics_include_session_id` | str | `null` | `OTEL_METRICS_INCLUDE_SESSION_ID` | Set to false to exclude session ID from metrics attributes (default: included) |
| `claude.env.otel_metrics_include_version` | bool | `null` | `OTEL_METRICS_INCLUDE_VERSION` | Set to true to include Claude Code version in metrics attributes (default: excluded) |
| `claude.env.slash_command_tool_char_budget` | str | `null` | `SLASH_COMMAND_TOOL_CHAR_BUDGET` | Override the character budget for skill metadata shown to the Skill tool |
| `claude.env.task_max_output_length` | str | `null` | `TASK_MAX_OUTPUT_LENGTH` | Removed in v2.1.277 and now a no-op, together with the TaskOutput tool it sized |
| `claude.env.use_builtin_ripgrep` | str | `null` | `USE_BUILTIN_RIPGREP` | Set to 0 to use system-installed rg instead of rg included with Claude Code |
| `claude.env.vertex_region_claude_3_5_haiku` | str | `null` | `VERTEX_REGION_CLAUDE_3_5_HAIKU` | Override region for Claude 3.5 Haiku when using Google Cloud's Agent Platform |
| `claude.env.vertex_region_claude_3_5_sonnet` | str | `null` | `VERTEX_REGION_CLAUDE_3_5_SONNET` | Override region for Claude 3.5 Sonnet when using Google Cloud's Agent Platform |
| `claude.env.vertex_region_claude_3_7_sonnet` | str | `null` | `VERTEX_REGION_CLAUDE_3_7_SONNET` | Override region for Claude 3.7 Sonnet when using Google Cloud's Agent Platform |
| `claude.env.vertex_region_claude_4_0_opus` | str | `null` | `VERTEX_REGION_CLAUDE_4_0_OPUS` | Override region for Claude Opus 4 when using Google Cloud's Agent Platform |
| `claude.env.vertex_region_claude_4_0_sonnet` | str | `null` | `VERTEX_REGION_CLAUDE_4_0_SONNET` | Override region for Claude Sonnet 4 when using Google Cloud's Agent Platform |
| `claude.env.vertex_region_claude_4_1_opus` | str | `null` | `VERTEX_REGION_CLAUDE_4_1_OPUS` | Override region for Claude Opus 4.1 when using Google Cloud's Agent Platform |
| `claude.env.vertex_region_claude_4_5_opus` | str | `null` | `VERTEX_REGION_CLAUDE_4_5_OPUS` | Override region for Claude Opus 4.5 when using Google Cloud's Agent Platform |
| `claude.env.vertex_region_claude_4_5_sonnet` | str | `null` | `VERTEX_REGION_CLAUDE_4_5_SONNET` | Override region for Claude Sonnet 4.5 when using Google Cloud's Agent Platform |
| `claude.env.vertex_region_claude_4_6_opus` | str | `null` | `VERTEX_REGION_CLAUDE_4_6_OPUS` | Override region for Claude Opus 4.6 when using Google Cloud's Agent Platform |
| `claude.env.vertex_region_claude_4_6_sonnet` | str | `null` | `VERTEX_REGION_CLAUDE_4_6_SONNET` | Override region for Claude Sonnet 4.6 when using Google Cloud's Agent Platform |
| `claude.env.vertex_region_claude_4_7_opus` | str | `null` | `VERTEX_REGION_CLAUDE_4_7_OPUS` | Override region for Claude Opus 4.7 when using Google Cloud's Agent Platform |
| `claude.env.vertex_region_claude_4_8_opus` | str | `null` | `VERTEX_REGION_CLAUDE_4_8_OPUS` | Override region for Claude Opus 4.8 when using Google Cloud's Agent Platform |
| `claude.env.vertex_region_claude_5_5_opus` | str | `null` | `VERTEX_REGION_CLAUDE_5_5_OPUS` | Override region for Claude Opus 5.5 when using Google Cloud's Agent Platform |
| `claude.env.vertex_region_claude_5_5_sonnet` | str | `null` | `VERTEX_REGION_CLAUDE_5_5_SONNET` | Override region for Claude Sonnet 5.5 when using Google Cloud's Agent Platform |
| `claude.env.vertex_region_claude_5_opus` | str | `null` | `VERTEX_REGION_CLAUDE_5_OPUS` | Override region for Claude Opus 5 when using Google Cloud's Agent Platform |
| `claude.env.vertex_region_claude_5_sonnet` | str | `null` | `VERTEX_REGION_CLAUDE_5_SONNET` | Override region for Claude Sonnet 5 when using Google Cloud's Agent Platform |
| `claude.env.vertex_region_claude_fable_5` | str | `null` | `VERTEX_REGION_CLAUDE_FABLE_5` | Override region for Claude Fable 5 when using Google Cloud's Agent Platform |
| `claude.env.vertex_region_claude_fable_5_1` | str | `null` | `VERTEX_REGION_CLAUDE_FABLE_5_1` | Override region for Claude Fable 5.1 when using Google Cloud's Agent Platform |
| `claude.env.vertex_region_claude_haiku_4_5` | str | `null` | `VERTEX_REGION_CLAUDE_HAIKU_4_5` | Override region for Claude Haiku 4.5 when using Google Cloud's Agent Platform |
| `claude.env.vertex_region_claude_haiku_5_5` | str | `null` | `VERTEX_REGION_CLAUDE_HAIKU_5_5` | Override region for Claude Haiku 5.5 when using Google Cloud's Agent Platform |

### `init`

```yaml
init:
  workdir:
    mode: clone
    repo_url: git@github.com:acme/app.git
    branch: main
    depth: 1
  git:
    user_name: Agent
    user_email: agent@example.com
  setup_commands:
    - echo hello
    - {run: make bootstrap, timeout_seconds: 600}
```

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `init.workdir.mode` | enum (empty, mounted, clone) | `"empty"` | - | `empty` creates a fresh directory, `mounted` uses an existing path, `clone` clones `repo_url`. |
| `init.workdir.path` | string | `""` | - | Absolute path of the workdir; required when mode is `mounted`. |
| `init.workdir.name` | string | `""` | - | Directory name under `paths.workdir_root`; empty uses the session id. |
| `init.workdir.repo_url` | string | `""` | - | Repository URL; required when mode is `clone`. |
| `init.workdir.branch` | string | `""` | - | Branch to clone; empty uses the remote default. |
| `init.workdir.depth` | integer | `0` | - | Shallow clone depth; 0 clones full history. |
| `init.workdir.submodules` | boolean | `false` | - | Initialise submodules after cloning. |
| `init.git.user_name` | string | `""` | - | `git config user.name`; empty leaves it unset. |
| `init.git.user_email` | string | `""` | - | `git config user.email`; empty leaves it unset. |
| `init.setup_commands` | list | `[]` | - | List run in the workdir before claude starts. Each item is a shell string or `{run, timeout_seconds: 600}`. |

### `prompts`

```yaml
prompts:
  global_claude_md: /etc/ccw/CLAUDE.md
  system_append: "Always run the tests before finishing."
  initial_prompt: {template: greet, variables: {who: team}}
  templates:
    greet:
      description: Greeting prompt
      variables: {who: {required: true}}
      text: "Hello {{who}}, please review the repository."
```

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `prompts.global_claude_md` | string | `""` | - | Path of a file whose content starts the session `CLAUDE.md`. |
| `prompts.system_append` | string | `""` | - | Text appended to the session `CLAUDE.md`. |
| `prompts.initial_prompt` | string or object | `""` | - | Prompt sent as the first turn: a string, or `{template: name, variables: {name: value}}` referencing `prompts.templates`. |
| `prompts.templates` | map | `{}` | - | Map of template name to `{description: "", variables: {name: {required: false, default: null}}, text: ""}`; `text` uses `{{name}}` placeholders, each must be declared in `variables`. |

### `mise`

```yaml
mise:
  tools: {node: "22", python: "3.12"}
  install_on_start: true
  trusted_paths: [/workdir]
  required: true
```

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `mise.tools` | map | `{}` | - | Map of tool to version installed globally, for example `{node: "22"}`. |
| `mise.config_file` | string | `""` | - | Path of a mise config copied in as the global config; empty for none. |
| `mise.install_on_start` | boolean | `true` | - | Run `mise install` for the global tools at service start. |
| `mise.install_workdir` | boolean | `true` | - | Run `mise install` in each new workdir that has a mise config. |
| `mise.trusted_paths` | list | `["/workdir"]` | - | List of absolute paths written to mise `trusted_config_paths`. |
| `mise.required` | boolean | `true` | - | Fail startup when installation fails; otherwise log and continue. |
| `mise.install_timeout_seconds` | number | `900` | - | Timeout of one `mise install` run (> 0). |
| `mise.global_config_path` | string | `"/home/agent/.config/mise/config.toml"` | - | Absolute path of the global mise config the service writes. |

### `plugins`

```yaml
plugins:
  marketplaces:
    - {name: team, source: "github:acme/claude-plugins"}
  install:
    - {plugin: "workbench@team", scope: user, required: true}
  disable: ["superpowers@claude-plugins-official"]
```

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `plugins.marketplaces` | list | `[]` | - | List of `{name, source}` marketplaces added before installing; `source` may be a mounted directory. |
| `plugins.install` | list | `[]` | - | List of `{plugin: name@marketplace, scope: user\|project\|local = user, required: true}`. The marketplace must be listed or well known (`claude-plugins-official`). |
| `plugins.disable` | list | `[]` | - | List of plugin ids (`name@marketplace`) to disable. |
| `plugins.timeout_seconds` | number | `180` | - | Timeout of one plugin CLI call (> 0). |

### `hooks`

```yaml
hooks:
  onTurnComplete:
    - {type: webhook, url: "https://example.com/turn-done", secret: "${HOOK_SECRET:-}", retries: 3}
  onAgentToolCall:
    - {type: command, run: "grep -q 'rm -rf' && { echo blocked >&2; exit 2; } || exit 0", matcher: Bash, blocking: true, on_error: deny}
```

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `hooks.onCreate` | list | `[]` | - | List of hook actions run after claude is ready; a blocking deny aborts creation. Item: command `{type: command, run, env: {}, timeout_seconds: 30, blocking: false, on_error: allow\|deny = allow, matcher}` or webhook `{type: webhook, url, secret, headers: {}, timeout_seconds: 10, retries: 3, blocking, on_error, matcher}`. See [Hooks](#hooks). |
| `hooks.onResume` | list | `[]` | - | List of hook actions run after a resumed claude is ready; a blocking deny aborts the resume. Item: command `{type: command, run, env: {}, timeout_seconds: 30, blocking: false, on_error: allow\|deny = allow, matcher}` or webhook `{type: webhook, url, secret, headers: {}, timeout_seconds: 10, retries: 3, blocking, on_error, matcher}`. See [Hooks](#hooks). |
| `hooks.onDestroy` | list | `[]` | - | List of hook actions run after claude stopped. Item: command `{type: command, run, env: {}, timeout_seconds: 30, blocking: false, on_error: allow\|deny = allow, matcher}` or webhook `{type: webhook, url, secret, headers: {}, timeout_seconds: 10, retries: 3, blocking, on_error, matcher}`. See [Hooks](#hooks). |
| `hooks.onTurn` | list | `[]` | - | List of hook actions run before the prompt is pasted; a blocking deny vetoes the turn. Item: command `{type: command, run, env: {}, timeout_seconds: 30, blocking: false, on_error: allow\|deny = allow, matcher}` or webhook `{type: webhook, url, secret, headers: {}, timeout_seconds: 10, retries: 3, blocking, on_error, matcher}`. See [Hooks](#hooks). |
| `hooks.onTurnComplete` | list | `[]` | - | List of hook actions run on every terminal turn state. Item: command `{type: command, run, env: {}, timeout_seconds: 30, blocking: false, on_error: allow\|deny = allow, matcher}` or webhook `{type: webhook, url, secret, headers: {}, timeout_seconds: 10, retries: 3, blocking, on_error, matcher}`. See [Hooks](#hooks). |
| `hooks.onStop` | list | `[]` | - | List of hook actions run when claude's Stop would end the running turn; a blocking block continues the turn, a defer holds it open. Item: command `{type: command, run, env: {}, timeout_seconds: 30, blocking: false, on_error: allow\|deny = allow, matcher}` or webhook `{type: webhook, url, secret, headers: {}, timeout_seconds: 10, retries: 3, blocking, on_error, matcher}`. See [`onStop`](#onstop). |
| `hooks.onTick` | list | `[]` | - | List of hook actions run on a timer while the session lives. Item: the usual command or webhook action plus `interval_seconds` (required, > 0) and `when: busy\|always = busy` (both allowed only here); no `matcher`. See [`onTick`](#ontick). |
| `hooks.onAgentResponse` | list | `[]` | - | List of hook actions run for assistant text read from the transcript. Item: command `{type: command, run, env: {}, timeout_seconds: 30, blocking: false, on_error: allow\|deny = allow, matcher}` or webhook `{type: webhook, url, secret, headers: {}, timeout_seconds: 10, retries: 3, blocking, on_error, matcher}`. See [Hooks](#hooks). |
| `hooks.onAgentToolCall` | list | `[]` | - | List of hook actions run on PreToolUse; a blocking deny blocks the tool call; `matcher` allowed. Item: command `{type: command, run, env: {}, timeout_seconds: 30, blocking: false, on_error: allow\|deny = allow, matcher}` or webhook `{type: webhook, url, secret, headers: {}, timeout_seconds: 10, retries: 3, blocking, on_error, matcher}`. See [Hooks](#hooks). |
| `hooks.onToolResult` | list | `[]` | - | List of hook actions run on PostToolUse and PostToolUseFailure; `matcher` allowed. Item: command `{type: command, run, env: {}, timeout_seconds: 30, blocking: false, on_error: allow\|deny = allow, matcher}` or webhook `{type: webhook, url, secret, headers: {}, timeout_seconds: 10, retries: 3, blocking, on_error, matcher}`. See [Hooks](#hooks). |
| `hooks.onSubagentStart` | list | `[]` | - | List of hook actions run when a subagent starts. Item: command `{type: command, run, env: {}, timeout_seconds: 30, blocking: false, on_error: allow\|deny = allow, matcher}` or webhook `{type: webhook, url, secret, headers: {}, timeout_seconds: 10, retries: 3, blocking, on_error, matcher}`. See [Hooks](#hooks). |
| `hooks.onSubagentStop` | list | `[]` | - | List of hook actions run when a subagent stops. Item: command `{type: command, run, env: {}, timeout_seconds: 30, blocking: false, on_error: allow\|deny = allow, matcher}` or webhook `{type: webhook, url, secret, headers: {}, timeout_seconds: 10, retries: 3, blocking, on_error, matcher}`. See [Hooks](#hooks). |
| `hooks.onCommand` | list | `[]` | - | List of hook actions run before a queued slash command is typed; a blocking deny vetoes the command. Item: command `{type: command, run, env: {}, timeout_seconds: 30, blocking: false, on_error: allow\|deny = allow, matcher}` or webhook `{type: webhook, url, secret, headers: {}, timeout_seconds: 10, retries: 3, blocking, on_error, matcher}`. See [Hooks](#hooks). |
| `hooks.onCommandComplete` | list | `[]` | - | List of hook actions run after a queued slash command completed, failed or was cancelled. Item: command `{type: command, run, env: {}, timeout_seconds: 30, blocking: false, on_error: allow\|deny = allow, matcher}` or webhook `{type: webhook, url, secret, headers: {}, timeout_seconds: 10, retries: 3, blocking, on_error, matcher}`. See [Hooks](#hooks). |
| `hooks.onInterrupt` | list | `[]` | - | List of hook actions run after a turn was interrupted. Item: command `{type: command, run, env: {}, timeout_seconds: 30, blocking: false, on_error: allow\|deny = allow, matcher}` or webhook `{type: webhook, url, secret, headers: {}, timeout_seconds: 10, retries: 3, blocking, on_error, matcher}`. See [Hooks](#hooks). |
| `hooks.onError` | list | `[]` | - | List of hook actions run on fatal pattern, crash, boot timeout, step failure or relay error. Item: command `{type: command, run, env: {}, timeout_seconds: 30, blocking: false, on_error: allow\|deny = allow, matcher}` or webhook `{type: webhook, url, secret, headers: {}, timeout_seconds: 10, retries: 3, blocking, on_error, matcher}`. See [Hooks](#hooks). |

### `sessions`

```yaml
sessions:
  max_sessions: 4
  idle_timeout_seconds: 0
  ask_user_question: forward
  resume_on_start: false
```

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `sessions.max_sessions` | integer | `4` | - | Concurrent live sessions, >= 1. |
| `sessions.idle_timeout_seconds` | number | `0` | - | Stop sessions idle this long; 0 never. |
| `sessions.turn_stall_seconds` | number | `1800` | - | Silence after which a running turn is reported as possibly stalled; 0 disables. |
| `sessions.interrupt_settle_seconds` | number | `20` | - | How long to wait for an interrupt to settle. |
| `sessions.max_restarts` | integer | `3` | - | Claude crash restarts per session before it is marked dead. |
| `sessions.ask_user_question` | enum (forward, deny, allow) | `"forward"` | - | `forward` surfaces AskUserQuestion as a question event, `deny` refuses it, `allow` lets claude ask in the TUI. |
| `sessions.turn_marker` | boolean | `true` | - | Tag each turn so completion can be correlated with its prompt. |
| `sessions.resume_on_start` | boolean | `false` | - | Resume known sessions when the service starts instead of listing them as stopped. |
| `sessions.defer_stop_on_background_tasks` | boolean | `false` | - | Keep the running turn open (`turn_deferred`) while a Stop reports non-empty `background_tasks`; a later Stop with none completes it. See [`onStop`](#onstop). |
| `sessions.probe_alive_token` | string | `"CCW-ALIVE"` | - | Non-empty token a probe asks Claude to reply with; an assistant message containing it answers the probe, and the default probe text names it. |
| `sessions.restart_continue_prompt` | string | `""` | - | Prompt pasted (with the same turn marker) into a resumed relaunch after claude crashed mid-turn, so the running turn continues instead of failing with `claude_exited` (a `turn_resumed` event); empty disables it. See "Crash during a turn" under `POST /v1/sessions/{id}/turns`. |
| `sessions.exit_when_last_destroyed` | boolean | `false` | - | Exit 0 through the normal shutdown (hooks drained) when a DELETE or the idle reaper leaves no active session and one was created in this process; the triggering DELETE still returns its full body first. |
| `sessions.exit_when_dead` | boolean | `false` | - | Exit 3 through the normal shutdown when a session reaches `dead`, after writing `session <id> dead: <last_fatal or "claude exited">` to `/dev/termination-log` when writable. |
| `sessions.commands.agent` | list of string | `[]` | - | Slash commands the main agent may queue through the ccw MCP tool; any of `compact`, `context`, `mcp`, `reload-plugins`, `reload-skills`. Empty allows none. |
| `sessions.commands.client` | list of string | `[]` | - | Slash commands API clients may queue; same names as `agent`. Empty allows none. |
| `sessions.commands.timeout_seconds` | map of number | `{"compact": 600, "default": 60}` | - | Seconds a command may run before it fails with `timeout`; keys are a command name or `default`, values > 0. |
| `sessions.commands.continue_prompt` | string | `"The slash commands you queued have run. Continue the task you were working on."` | - | Non-empty text that resumes the agent after its queued commands ran, followed by a blank line and the `Command results: ...` line (see [`turn_resumed`](#turn_resumed)). |
| `sessions.commands.allow_force` | boolean | `false` | - | Allow `force: true` on `reload-plugins` (typed as `--force`). |
| `sessions.commands.quiet_seconds` | number | `1.5` | - | Seconds of PTY silence to wait for before typing a command, >= 0. |
| `sessions.auto_compact.idle_seconds` | number | `0` | - | Run `/compact` after the session stayed idle this long following a turn; 0 is off. Must be below `idle_timeout_seconds` when both are set. |
| `sessions.auto_compact.instructions` | string | `""` | - | One-line focus instructions passed to the automatic `/compact` (max 2000 characters, no control characters); empty sends none. |
| `sessions.auto_compact.min_context_tokens` | integer | `0` | - | Skip the automatic compact while the last main-chain context (input + cache read + cache creation tokens) is below this many tokens, >= 0; 0 compacts whatever the size. |

Idle auto-compact (`sessions.auto_compact`): a session that ended a turn (any terminal state) and then saw no activity for `idle_seconds` queues one `/compact` with `origin: "idle"`, run on the idle path (the session is `busy` while it runs). It fires at most once per idle gap; the next turn end re-arms it, and a session that never ran a turn is never compacted. A turn or a client or agent command starting cancels the pending timer. With `min_context_tokens` set, a context below it queues nothing (a debug log line, no event). Events of an idle-origin command (its `command_*` events and everything emitted while it runs) do not count as activity, neither for the auto-compact clock nor for `idle_timeout_seconds`, so an automatic compact never keeps a session alive. Exit policies are unaffected, and shutdown during an automatic compact abandons it.

### `limits`

```yaml
limits:
  max_prompt_bytes: 1048576
  sse_replay: 100
  shutdown_timeout_seconds: 10
```

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `limits.max_prompt_bytes` | integer | `1048576` | - | Largest accepted turn prompt in bytes. |
| `limits.max_file_bytes` | integer | `262144` | - | Largest accepted turn file in bytes. |
| `limits.max_files_per_turn` | integer | `16` | - | Most files accepted with one turn. |
| `limits.turn_max_seconds` | number | `0` | - | Hard cap per turn; 0 for none. |
| `limits.event_ring_size` | integer | `1000` | - | Events kept in memory per session. |
| `limits.event_log_max_bytes` | integer | `52428800` | - | On-disk `events.jsonl` cap per session in bytes; 0 for unlimited. |
| `limits.sse_replay` | integer | `100` | - | Events replayed to a new SSE subscriber without `Last-Event-ID`. |
| `limits.sse_heartbeat_seconds` | number | `15` | - | Interval of SSE heartbeat comments (> 0). |
| `limits.shutdown_timeout_seconds` | number | `10` | - | Bounded graceful shutdown: seconds allowed to stop sessions on SIGTERM. |

## Claude Code environment

`claude.env` is a typed model of the environment variables Claude Code reads. Each variable is one
optional field named after it, lowercased (`claude_code_oauth_token` is `CLAUDE_CODE_OAUTH_TOKEN`);
the table below lists all of them with their group, checked against the Claude Code env-var docs.

- **Typed fields.** `str` values must be non-empty, `int` values positive. An empty string means
  unset (so `${VAR:-}` is safe), and unset fields are never exported.
- **Booleans.** `true` exports `1`; `false` exports nothing, which is how a per-session override
  turns a global flag off.
- **Secrets.** Fields marked `yes` below are masked as `***` in `ccw check-config`, in session
  snapshots and in every API response, and their values are scrubbed from event payloads, the
  transcript stream and logs.
- **OAuth vs API key.** If both `claude_code_oauth_token` and `anthropic_api_key` are set, the OAuth
  token wins: `ANTHROPIC_API_KEY` is not exported and a warning is logged at start.
- **Provider exclusivity.** At most one of `claude_code_use_bedrock`, `claude_code_use_vertex`,
  `claude_code_use_foundry` and `claude_code_use_anthropic_aws` may be true.
- **Managed `CLAUDE_CONFIG_DIR`.** The service sets it per session to
  `<paths.sessions_root>/<session_id>/config`; `claude_config_dir` cannot be set.
- **`extra`.** `claude.env.extra` is a map of any other variable, passed verbatim. Names match
  `[A-Z_][A-Z0-9_]*`; a variable that has a typed field must use the field, `CLAUDE_CONFIG_DIR` is
  managed, and `CCW_*`, `TERM` and `HOME` are reserved. Values of names containing TOKEN, KEY,
  SECRET, PASSWORD, AUTH or CREDENTIAL are treated as secrets.
- **Per-session override.** `POST /v1/sessions` and `POST /v1/sessions/{id}/resume` accept
  `{"env": {...}}`, a partial `claude.env` merged over the global one: typed fields replace,
  `false` or `null` unsets a global value, `extra` merges key-wise. Non-secret overrides are
  persisted with the session. **Secret overrides are never persisted**: resuming a session that
  was created with them requires resending them in the resume request body.
- **Redaction.** Secret values are masked everywhere they could surface, including in
  `claude.mcp_servers` headers and secret-looking MCP env values, and in hook webhook secrets.

| Field | Env var | Type | Secret | Group | Description |
|---|---|---|---|---|---|
| `anthropic_api_key` | `ANTHROPIC_API_KEY` | secret | yes | auth | API key sent as X-Api-Key header |
| `anthropic_auth_token` | `ANTHROPIC_AUTH_TOKEN` | secret | yes | auth | Custom value for the Authorization header (the value you set here will be prefixed with Bearer ) |
| `anthropic_aws_api_key` | `ANTHROPIC_AWS_API_KEY` | secret | yes | auth | Workspace API key for Claude Platform on AWS, generated in the AWS Console |
| `anthropic_aws_base_url` | `ANTHROPIC_AWS_BASE_URL` | str | no | provider | Override the Claude Platform on AWS endpoint URL. Use for custom regions or when routing through an LLM gateway |
| `anthropic_aws_workspace_id` | `ANTHROPIC_AWS_WORKSPACE_ID` | str | no | provider | Required for Claude Platform on AWS. Sent on every request as the anthropic-workspace-id header |
| `anthropic_base_url` | `ANTHROPIC_BASE_URL` | str | no | behaviour | Override the API endpoint to route requests through a proxy or gateway |
| `anthropic_bedrock_base_url` | `ANTHROPIC_BEDROCK_BASE_URL` | str | no | provider | Override the Amazon Bedrock endpoint URL. Use for custom Amazon Bedrock endpoints or when routing through an LLM gateway |
| `anthropic_bedrock_mantle_base_url` | `ANTHROPIC_BEDROCK_MANTLE_BASE_URL` | str | no | provider | Override the Amazon Bedrock Mantle endpoint URL. See Mantle endpoint |
| `anthropic_bedrock_region_prefix` | `ANTHROPIC_BEDROCK_REGION_PREFIX` | str | no | provider | Cross-region inference profile prefix (us, eu, apac, jp, au, or global) Claude Code tries first instead of the one de... |
| `anthropic_bedrock_service_tier` | `ANTHROPIC_BEDROCK_SERVICE_TIER` | str | no | provider | Amazon Bedrock service tier (default, flex, or priority) |
| `anthropic_betas` | `ANTHROPIC_BETAS` | str | no | model | Comma-separated list of additional anthropic-beta header values to include in API requests |
| `anthropic_custom_headers` | `ANTHROPIC_CUSTOM_HEADERS` | secret | yes | behaviour | Custom headers to add to requests (Name: Value format, newline-separated for multiple headers) |
| `anthropic_custom_model_option` | `ANTHROPIC_CUSTOM_MODEL_OPTION` | str | no | model | Model ID to add as a custom entry in the /model picker |
| `anthropic_custom_model_option_description` | `ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION` | str | no | model | Display description for the custom model entry in the /model picker |
| `anthropic_custom_model_option_name` | `ANTHROPIC_CUSTOM_MODEL_OPTION_NAME` | str | no | model | Display name for the custom model entry in the /model picker |
| `anthropic_custom_model_option_supported_capabilities` | `ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES` | str | no | model | Comma-separated list of capabilities the custom model supports, for example effort,thinking |
| `anthropic_default_fable_model` | `ANTHROPIC_DEFAULT_FABLE_MODEL` | str | no | model | Model ID that the fable alias resolves to, and the ID Claude Code recognizes as a Fable model for automatic model fal... |
| `anthropic_default_fable_model_description` | `ANTHROPIC_DEFAULT_FABLE_MODEL_DESCRIPTION` | str | no | model | Display description for the pinned Fable model in the /model picker |
| `anthropic_default_fable_model_name` | `ANTHROPIC_DEFAULT_FABLE_MODEL_NAME` | str | no | model | Display name for the pinned Fable model in the /model picker |
| `anthropic_default_fable_model_supported_capabilities` | `ANTHROPIC_DEFAULT_FABLE_MODEL_SUPPORTED_CAPABILITIES` | str | no | model | Comma-separated list of capabilities the pinned Fable model supports, for example effort,thinking |
| `anthropic_default_haiku_model` | `ANTHROPIC_DEFAULT_HAIKU_MODEL` | str | no | model | Model ID that the haiku alias resolves to, also used for background functionality |
| `anthropic_default_haiku_model_description` | `ANTHROPIC_DEFAULT_HAIKU_MODEL_DESCRIPTION` | str | no | model | Display description for the pinned Haiku model in the /model picker |
| `anthropic_default_haiku_model_name` | `ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME` | str | no | model | Display name for the pinned Haiku model in the /model picker |
| `anthropic_default_haiku_model_supported_capabilities` | `ANTHROPIC_DEFAULT_HAIKU_MODEL_SUPPORTED_CAPABILITIES` | str | no | model | Comma-separated list of capabilities the pinned Haiku model supports, for example effort,thinking |
| `anthropic_default_model` | `ANTHROPIC_DEFAULT_MODEL` | str | no | model | Model that new sessions start on by default |
| `anthropic_default_opus_model` | `ANTHROPIC_DEFAULT_OPUS_MODEL` | str | no | model | Model ID that the opus alias resolves to, and that opusplan uses while Plan Mode is active |
| `anthropic_default_opus_model_description` | `ANTHROPIC_DEFAULT_OPUS_MODEL_DESCRIPTION` | str | no | model | Display description for the pinned Opus model in the /model picker |
| `anthropic_default_opus_model_name` | `ANTHROPIC_DEFAULT_OPUS_MODEL_NAME` | str | no | model | Display name for the pinned Opus model in the /model picker |
| `anthropic_default_opus_model_supported_capabilities` | `ANTHROPIC_DEFAULT_OPUS_MODEL_SUPPORTED_CAPABILITIES` | str | no | model | Comma-separated list of capabilities the pinned Opus model supports, for example effort,thinking |
| `anthropic_default_sonnet_model` | `ANTHROPIC_DEFAULT_SONNET_MODEL` | str | no | model | Model ID that the sonnet alias resolves to, and that opusplan uses when Plan Mode is not active |
| `anthropic_default_sonnet_model_description` | `ANTHROPIC_DEFAULT_SONNET_MODEL_DESCRIPTION` | str | no | model | Display description for the pinned Sonnet model in the /model picker |
| `anthropic_default_sonnet_model_name` | `ANTHROPIC_DEFAULT_SONNET_MODEL_NAME` | str | no | model | Display name for the pinned Sonnet model in the /model picker |
| `anthropic_default_sonnet_model_supported_capabilities` | `ANTHROPIC_DEFAULT_SONNET_MODEL_SUPPORTED_CAPABILITIES` | str | no | model | Comma-separated list of capabilities the pinned Sonnet model supports, for example effort,thinking |
| `anthropic_federation_rule_id` | `ANTHROPIC_FEDERATION_RULE_ID` | str | no | behaviour | Federation rule ID for Workload Identity Federation |
| `anthropic_foundry_api_key` | `ANTHROPIC_FOUNDRY_API_KEY` | secret | yes | auth | API key for Microsoft Foundry authentication (see Microsoft Foundry) |
| `anthropic_foundry_auth_token` | `ANTHROPIC_FOUNDRY_AUTH_TOKEN` | secret | yes | auth | Bearer token for Microsoft Foundry authentication, such as a Microsoft Entra access token |
| `anthropic_foundry_base_url` | `ANTHROPIC_FOUNDRY_BASE_URL` | str | no | provider | Full base URL for the Microsoft Foundry resource (for example, https://my-resource.services.ai.azure.com/anthropic) |
| `anthropic_foundry_resource` | `ANTHROPIC_FOUNDRY_RESOURCE` | str | no | provider | Microsoft Foundry resource name (for example, my-resource) |
| `anthropic_model` | `ANTHROPIC_MODEL` | str | no | model | Name of the model setting to use (see Model Configuration) |
| `anthropic_organization_id` | `ANTHROPIC_ORGANIZATION_ID` | str | no | behaviour | Organization ID for Workload Identity Federation |
| `anthropic_profile` | `ANTHROPIC_PROFILE` | str | no | behaviour | Name of the Anthropic profile to authenticate with, such as one created by ant auth login or by signing in to a Conso... |
| `anthropic_small_fast_model` | `ANTHROPIC_SMALL_FAST_MODEL` | str | no | model | [DEPRECATED] Name of Haiku-class model for background tasks |
| `anthropic_small_fast_model_aws_region` | `ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION` | str | no | model | Override AWS region for the Haiku-class model when using Amazon Bedrock or Amazon Bedrock Mantle |
| `anthropic_vertex_base_url` | `ANTHROPIC_VERTEX_BASE_URL` | str | no | provider | Override Google Cloud's Agent Platform endpoint URL. Use for custom Google Cloud's Agent Platform endpoints or when r... |
| `anthropic_vertex_project_id` | `ANTHROPIC_VERTEX_PROJECT_ID` | str | no | provider | GCP project ID that Google Cloud's Agent Platform requests are addressed to |
| `anthropic_workspace_id` | `ANTHROPIC_WORKSPACE_ID` | str | no | behaviour | Workspace ID for workload identity federation |
| `api_force_idle_timeout` | `API_FORCE_IDLE_TIMEOUT` | int | no | behaviour | Override the 5-minute body idle timeout that aborts a streaming model response when no bytes arrive |
| `api_timeout_ms` | `API_TIMEOUT_MS` | int | no | behaviour | Time in milliseconds that each API request attempt has to receive its response headers (default: 600000, or 10 minute... |
| `aws_bearer_token_bedrock` | `AWS_BEARER_TOKEN_BEDROCK` | secret | yes | provider | Amazon Bedrock API key for authentication (see Amazon Bedrock API keys) |
| `aws_profile` | `AWS_PROFILE` | str | no | provider | AWS profile name for Amazon Bedrock credentials |
| `aws_region` | `AWS_REGION` | str | no | provider | AWS region for Amazon Bedrock |
| `bash_default_timeout_ms` | `BASH_DEFAULT_TIMEOUT_MS` | int | no | behaviour | Default timeout for a foreground Bash or PowerShell tool command, in milliseconds (default: 120000, or 2 minutes) |
| `bash_max_output_length` | `BASH_MAX_OUTPUT_LENGTH` | int | no | behaviour | Maximum number of characters of bash output that Claude Code reads back into a command's result (default: 30000; maxi... |
| `bash_max_timeout_ms` | `BASH_MAX_TIMEOUT_MS` | int | no | behaviour | Maximum timeout the model can set for a foreground Bash or PowerShell tool command, in milliseconds (default: 600000,... |
| `beta_tracing_endpoint` | `BETA_TRACING_ENDPOINT` | str | no | behaviour | OTLP/HTTP endpoint for detailed beta tracing: with ENABLE_BETA_TRACING_DETAILED=1, logs and traces go there instead o... |
| `ccr_force_bundle` | `CCR_FORCE_BUNDLE` | bool | no | behaviour | Set to 1 to force claude --cloud to bundle and upload your local repository instead of cloning from its remote |
| `claudecode` | `CLAUDECODE` | str | no | behaviour | Set to 1 in subprocesses Claude Code spawns (Bash and PowerShell tools, tmux sessions, hook commands, status line com... |
| `claude_afk_countdown_ms` | `CLAUDE_AFK_COUNTDOWN_MS` | int | no | behaviour | How many milliseconds before auto-continue the on-screen countdown appears on an unanswered AskUserQuestion dialog |
| `claude_afk_timeout_ms` | `CLAUDE_AFK_TIMEOUT_MS` | int | no | behaviour | How many milliseconds of idle time before an unanswered AskUserQuestion dialog auto-continues without you |
| `claude_agent_sdk_disable_builtin_agents` | `CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS` | bool | no | behaviour | Set to 1 to disable all built-in subagent types such as Explore and Plan |
| `claude_agent_sdk_mcp_no_prefix` | `CLAUDE_AGENT_SDK_MCP_NO_PREFIX` | bool | no | mcp | Set to 1 to skip the mcp__<server>__ prefix on tool names from SDK-created MCP servers |
| `claude_async_agent_stall_timeout_ms` | `CLAUDE_ASYNC_AGENT_STALL_TIMEOUT_MS` | int | no | behaviour | Stall timeout in milliseconds for subagents |
| `claude_autocompact_pct_override` | `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` | int | no | model | Set the percentage (1-100) of the auto-compact window at which auto-compaction triggers |
| `claude_auto_background_tasks` | `CLAUDE_AUTO_BACKGROUND_TASKS` | bool | no | behaviour | Set to 1 to force-enable automatic backgrounding of long-running agent tasks |
| `claude_ax_prepark_ms` | `CLAUDE_AX_PREPARK_MS` | int | no | behaviour | In screen reader mode, how many milliseconds Claude Code waits before it writes a new or changed line |
| `claude_ax_screen_reader` | `CLAUDE_AX_SCREEN_READER` | bool | no | behaviour | Set to 1 to render screen-reader friendly output: flat text without decorative borders or animations |
| `claude_ax_startup_quiet_ms` | `CLAUDE_AX_STARTUP_QUIET_MS` | int | no | behaviour | In screen reader mode, how many milliseconds Claude Code holds the first interface render after the startup confirmat... |
| `claude_bash_maintain_project_working_dir` | `CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR` | str | no | behaviour | Return to the original working directory after each Bash or PowerShell command in the main session |
| `claude_byte_stream_idle_timeout_ms` | `CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS` | int | no | behaviour | Timeout in milliseconds for the byte-level streaming idle watchdog; when set, it takes precedence over CLAUDE_STREAM_... |
| `claude_client_presence_file` | `CLAUDE_CLIENT_PRESENCE_FILE` | str | no | behaviour | Path to a file that an external tool, such as a screen-lock listener, creates when you unlock your screen and deletes... |
| `claude_code_accessibility` | `CLAUDE_CODE_ACCESSIBILITY` | bool | no | behaviour | Set to 1 to keep the native terminal cursor visible and disable the inverted-text cursor indicator |
| `claude_code_additional_directories_claude_md` | `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD` | bool | no | behaviour | Set to 1 to load memory files from directories specified with --add-dir |
| `claude_code_alt_screen_full_repaint` | `CLAUDE_CODE_ALT_SCREEN_FULL_REPAINT` | bool | no | behaviour | Set to 1 to repaint the entire screen on every frame in fullscreen rendering instead of sending incremental updates |
| `claude_code_always_enable_effort` | `CLAUDE_CODE_ALWAYS_ENABLE_EFFORT` | bool | no | model | Set to 1 to send the effort parameter with every request, even when Claude Code does not recognize the model ID as ef... |
| `claude_code_api_key_helper_ttl_ms` | `CLAUDE_CODE_API_KEY_HELPER_TTL_MS` | int | no | auth | Interval in milliseconds at which credentials should be refreshed (when using apiKeyHelper) |
| `claude_code_artifact_auto_open` | `CLAUDE_CODE_ARTIFACT_AUTO_OPEN` | str | no | behaviour | Set to 0 to stop Claude Code from opening the browser automatically when a new artifact is published |
| `claude_code_artifact_comments` | `CLAUDE_CODE_ARTIFACT_COMMENTS` | str | no | behaviour | Set to 0 to stop Claude reading and replying to comments on an artifact |
| `claude_code_artifact_comments_autoreact` | `CLAUDE_CODE_ARTIFACT_COMMENTS_AUTOREACT` | str | no | behaviour | Set to 0 to stop Claude replying on its own to comments sent to it |
| `claude_code_artifact_publish_edits` | `CLAUDE_CODE_ARTIFACT_PUBLISH_EDITS` | str | no | behaviour | Set to 0 to stop Claude from applying small changes to an artifact's file as part of publishing it |
| `claude_code_attribution_header` | `CLAUDE_CODE_ATTRIBUTION_HEADER` | str | no | behaviour | Set to 0 to omit the attribution block, which carries the client version and a prompt fingerprint, from the start of ... |
| `claude_code_auto_background_worker_checkin_seconds` | `CLAUDE_CODE_AUTO_BACKGROUND_WORKER_CHECKIN_SECONDS` | str | no | behaviour | Removed in v2.1.283. Use CLAUDE_CODE_WORKER_CHECKIN_SCHEDULE instead |
| `claude_code_auto_compact_window` | `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | str | no | model | Set the auto-compact window, how full the context gets before compaction, from 100000 to 1000000 tokens |
| `claude_code_auto_connect_ide` | `CLAUDE_CODE_AUTO_CONNECT_IDE` | str | no | behaviour | Override automatic IDE connection |
| `claude_code_auto_mode_model` | `CLAUDE_CODE_AUTO_MODE_MODEL` | str | no | model | Sets the model for the classifier requests Claude Code sends in auto mode |
| `claude_code_auto_mode_server` | `CLAUDE_CODE_AUTO_MODE_SERVER` | str | no | behaviour | Controls whether Claude Code asks the server to review auto mode actions |
| `claude_code_aws_chain_resolve_timeout_ms` | `CLAUDE_CODE_AWS_CHAIN_RESOLVE_TIMEOUT_MS` | int | no | behaviour | Time in milliseconds Claude Code waits for the AWS default credential provider chain to produce credentials before th... |
| `claude_code_bash_edit_diff` | `CLAUDE_CODE_BASH_EDIT_DIFF` | str | no | behaviour | Set to 0 to turn off the diff of the files that changed while a Bash command ran, or 1 to record it in every permissi... |
| `claude_code_bg_tasks_report_running` | `CLAUDE_CODE_BG_TASKS_REPORT_RUNNING` | str | no | behaviour | Set to 0 to make a non-interactive session report an idle status to its host at every turn end, even while background... |
| `claude_code_bridge_session_id` | `CLAUDE_CODE_BRIDGE_SESSION_ID` | str | no | behaviour | Set automatically in Bash tool and hook command subprocesses while the session has an active Remote Control connectio... |
| `claude_code_bs_as_ctrl_backspace` | `CLAUDE_CODE_BS_AS_CTRL_BACKSPACE` | str | no | behaviour | Set to 0 to make Claude Code read the 0x08 byte, also written ^H, as plain Backspace, or 1 to read it as Ctrl+Backspace |
| `claude_code_cert_store` | `CLAUDE_CODE_CERT_STORE` | str | no | network | Comma-separated list of CA certificate sources for TLS connections |
| `claude_code_child_session` | `CLAUDE_CODE_CHILD_SESSION` | str | no | behaviour | Set to 1 in subprocesses Claude Code spawns via the Bash, PowerShell, and Monitor tools, hook commands, and status li... |
| `claude_code_client_cert` | `CLAUDE_CODE_CLIENT_CERT` | str | no | network | Path to client certificate file for mTLS authentication |
| `claude_code_client_key` | `CLAUDE_CODE_CLIENT_KEY` | str | no | behaviour | Path to client private key file for mTLS authentication |
| `claude_code_client_key_passphrase` | `CLAUDE_CODE_CLIENT_KEY_PASSPHRASE` | secret | yes | behaviour | Passphrase for encrypted CLAUDE_CODE_CLIENT_KEY (optional) |
| `claude_code_connect_timeout_ms` | `CLAUDE_CODE_CONNECT_TIMEOUT_MS` | str | no | behaviour | Removed in v2.1.186 and now a no-op |
| `claude_code_debug_logs_dir` | `CLAUDE_CODE_DEBUG_LOGS_DIR` | str | no | behaviour | Override the debug log file path |
| `claude_code_debug_log_level` | `CLAUDE_CODE_DEBUG_LOG_LEVEL` | str | no | behaviour | Minimum log level written to the debug log file |
| `claude_code_disable_1m_context` | `CLAUDE_CODE_DISABLE_1M_CONTEXT` | bool | no | model | Set to 1 to turn off 1M context window support |
| `claude_code_disable_adaptive_thinking` | `CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING` | bool | no | model | Set to 1 to disable adaptive reasoning on Opus 4.6 and Sonnet 4.6 and fall back to the fixed thinking budget controll... |
| `claude_code_disable_admin_env_union` | `CLAUDE_CODE_DISABLE_ADMIN_ENV_UNION` | bool | no | behaviour | Set to 1 to stop Claude Code from merging managed settings env blocks per key across admin sources, so only the highe... |
| `claude_code_disable_advisor_tool` | `CLAUDE_CODE_DISABLE_ADVISOR_TOOL` | bool | no | behaviour | Set to 1 to disable the advisor tool |
| `claude_code_disable_agent_view` | `CLAUDE_CODE_DISABLE_AGENT_VIEW` | bool | no | behaviour | Set to 1 to turn off background agents and agent view: claude agents, --bg, /background, and the on-demand supervisor |
| `claude_code_disable_alternate_screen` | `CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN` | bool | no | behaviour | Set to 1 to disable fullscreen rendering and use the classic main-screen renderer |
| `claude_code_disable_artifact` | `CLAUDE_CODE_DISABLE_ARTIFACT` | bool | no | behaviour | Set to 1 to turn off the Artifact tool, which publishes session output as a private web page on claude.ai |
| `claude_code_disable_attachments` | `CLAUDE_CODE_DISABLE_ATTACHMENTS` | bool | no | behaviour | Set to 1 to disable attachment processing |
| `claude_code_disable_auth_refresh_lock` | `CLAUDE_CODE_DISABLE_AUTH_REFRESH_LOCK` | bool | no | behaviour | Set to 1 to make a Claude Code process run its gcpAuthRefresh or awsAuthRefresh command itself instead of waiting whi... |
| `claude_code_disable_auto_memory` | `CLAUDE_CODE_DISABLE_AUTO_MEMORY` | bool | no | privacy | Set to 1 to disable auto memory |
| `claude_code_disable_background_tasks` | `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS` | bool | no | behaviour | Set to 1 to disable all background task functionality, including the run_in_background parameter on Bash and subagent... |
| `claude_code_disable_bedrock_content_type_default` | `CLAUDE_CODE_DISABLE_BEDROCK_CONTENT_TYPE_DEFAULT` | bool | no | behaviour | Set to 1 to stop Claude Code from treating an Amazon Bedrock streaming response with a missing or empty Content-Type ... |
| `claude_code_disable_bedrock_content_type_guard` | `CLAUDE_CODE_DISABLE_BEDROCK_CONTENT_TYPE_GUARD` | bool | no | behaviour | Set to 1 to skip the check that an Amazon Bedrock streaming response carries the application/vnd.amazon.eventstream c... |
| `claude_code_disable_bg_exit_handoff` | `CLAUDE_CODE_DISABLE_BG_EXIT_HANDOFF` | bool | no | behaviour | Set to 1 to stop a background session's running background shell commands, dynamic workflows, and, as of v2.1.198, ba... |
| `claude_code_disable_bg_shell_pressure_reap` | `CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP` | bool | no | behaviour | Set to 1 to stop Claude Code from terminating background shell commands under memory pressure |
| `claude_code_disable_bundled_skills` | `CLAUDE_CODE_DISABLE_BUNDLED_SKILLS` | bool | no | behaviour | Set to 1 to disable the skills and workflows included with Claude Code: bundled skills and workflows are removed enti... |
| `claude_code_disable_cfc_prompt` | `CLAUDE_CODE_DISABLE_CFC_PROMPT` | bool | no | behaviour | Set to 1 to keep the Claude in Chrome browser tools available while omitting the Chrome section of the system prompt ... |
| `claude_code_disable_claude_mds` | `CLAUDE_CODE_DISABLE_CLAUDE_MDS` | bool | no | behaviour | Set to 1 to prevent loading any CLAUDE.md memory files into context, including user, project, and auto memory files |
| `claude_code_disable_cron` | `CLAUDE_CODE_DISABLE_CRON` | bool | no | behaviour | Set to 1 to disable session-scoped scheduled tasks |
| `claude_code_disable_dangerous_rm_timeout` | `CLAUDE_CODE_DISABLE_DANGEROUS_RM_TIMEOUT` | bool | no | behaviour | Set to 1 to turn off the time limit on critical-path removal prompts |
| `claude_code_disable_experimental_betas` | `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` | bool | no | model | Set to 1 to strip pre-release anthropic-beta request headers, the body fields that pair with them, and beta tool-sche... |
| `claude_code_disable_explore_plan_agents` | `CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS` | bool | no | behaviour | Set to 1 to disable the built-in Explore and Plan subagents |
| `claude_code_disable_fast_mode` | `CLAUDE_CODE_DISABLE_FAST_MODE` | bool | no | model | Set to 1 to disable fast mode |
| `claude_code_disable_feedback_survey` | `CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY` | bool | no | privacy | Set to 1 to disable the "How is Claude doing?" session quality surveys |
| `claude_code_disable_file_checkpointing` | `CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING` | bool | no | behaviour | Set to 1 to disable file checkpointing |
| `claude_code_disable_file_mentions` | `CLAUDE_CODE_DISABLE_FILE_MENTIONS` | bool | no | behaviour | Set to 1 to stop Claude Code from expanding @ mentions of files and directories |
| `claude_code_disable_git_instructions` | `CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS` | bool | no | behaviour | Set to 1 to remove built-in commit and PR workflow instructions and the git status snapshot from Claude's context |
| `claude_code_disable_inline_shell_rm_prompt` | `CLAUDE_CODE_DISABLE_INLINE_SHELL_RM_PROMPT` | bool | no | behaviour | Set to 1 to stop Claude Code from reading scripts passed to a shell with -c, such as bash -c 'rm -rf ~', for critical... |
| `claude_code_disable_legacy_model_remap` | `CLAUDE_CODE_DISABLE_LEGACY_MODEL_REMAP` | bool | no | model | Set to 1 to prevent automatic remapping of Opus 4 and Opus 4.1 to the current Opus version on the Anthropic API. Use ... |
| `claude_code_disable_model_access_fallback` | `CLAUDE_CODE_DISABLE_MODEL_ACCESS_FALLBACK` | bool | no | model | Set to 1 to stop Claude Code on Amazon Bedrock and Google Cloud's Agent Platform from switching to an older model whe... |
| `claude_code_disable_mounted_drive_rm_prompt` | `CLAUDE_CODE_DISABLE_MOUNTED_DRIVE_RM_PROMPT` | bool | no | behaviour | Set to 1 to turn off the mounted drive prompt for paths such as /Volumes/USB. Set it in the environment that launches... |
| `claude_code_disable_mouse` | `CLAUDE_CODE_DISABLE_MOUSE` | bool | no | behaviour | Set to 1 to disable mouse tracking in fullscreen rendering |
| `claude_code_disable_mouse_clicks` | `CLAUDE_CODE_DISABLE_MOUSE_CLICKS` | bool | no | behaviour | Set to 1 to disable click, drag, and hover handling in fullscreen rendering while keeping mouse-wheel scrolling |
| `claude_code_disable_mtls_reload_on_stale_connection` | `CLAUDE_CODE_DISABLE_MTLS_RELOAD_ON_STALE_CONNECTION` | bool | no | network | Set to 1 to stop Claude Code from re-reading the mTLS client certificate and key when an API request fails with a con... |
| `claude_code_disable_nonessential_traffic` | `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | bool | no | privacy | Set to any non-empty value, such as 1, to disable nonessential network traffic: auto-updates, telemetry, error report... |
| `claude_code_disable_nonstreaming_fallback` | `CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK` | bool | no | behaviour | Set to 1 to disable the non-streaming fallback when a streaming request fails mid-stream |
| `claude_code_disable_notification_presence_check` | `CLAUDE_CODE_DISABLE_NOTIFICATION_PRESENCE_CHECK` | bool | no | behaviour | Set to 1 to send the PushNotification tool's desktop notification even while you are typing in or focused on the term... |
| `claude_code_disable_official_marketplace_autoinstall` | `CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL` | bool | no | plugins | Set to 1 to disable automatic registration of the official plugin marketplace |
| `claude_code_disable_permission_prompt_notify_hooks` | `CLAUDE_CODE_DISABLE_PERMISSION_PROMPT_NOTIFY_HOOKS` | bool | no | behaviour | Set to 1 to stop Claude Code from running your Notification hooks for unanswered permission requests in sessions wher... |
| `claude_code_disable_policy_skills` | `CLAUDE_CODE_DISABLE_POLICY_SKILLS` | bool | no | behaviour | Set to 1 to skip loading skills from the system-wide managed skills directory |
| `claude_code_disable_powershell_cmd_rm_deny` | `CLAUDE_CODE_DISABLE_POWERSHELL_CMD_RM_DENY` | bool | no | behaviour | Set to 1 to turn off the PowerShell tool check that denies the cmd built-ins rd, rmdir, del, and erase on a system pa... |
| `claude_code_disable_refusal_fallback` | `CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK` | bool | no | behaviour | Set to 1 to turn off the automatic model switch when a safety classifier flags a request, the behavior the switchMode... |
| `claude_code_disable_structured_outputs` | `CLAUDE_CODE_DISABLE_STRUCTURED_OUTPUTS` | bool | no | behaviour | Set to 1 to stop Claude Code from sending the structured-output output_config.format field and the anthropic-beta val... |
| `claude_code_disable_substitution_rm_prompt` | `CLAUDE_CODE_DISABLE_SUBSTITUTION_RM_PROMPT` | bool | no | behaviour | Set to 1 to turn off the critical-path check for a recursive rm whose target is entirely the output of a command subs... |
| `claude_code_disable_terminal_title` | `CLAUDE_CODE_DISABLE_TERMINAL_TITLE` | bool | no | behaviour | Set to 1 to disable automatic terminal title updates based on conversation context |
| `claude_code_disable_thinking` | `CLAUDE_CODE_DISABLE_THINKING` | bool | no | model | Set to 1 to omit the thinking parameter from API requests entirely |
| `claude_code_disable_unknown_model_window_enforcement` | `CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT` | bool | no | model | Set to 1 to skip proactive auto-compaction when Claude Code doesn't recognize the model ID, such as an LLM gateway alias |
| `claude_code_disable_virtual_scroll` | `CLAUDE_CODE_DISABLE_VIRTUAL_SCROLL` | bool | no | behaviour | Set to 1 to disable virtual scrolling in fullscreen rendering and render every message in the transcript |
| `claude_code_disable_web_fetch` | `CLAUDE_CODE_DISABLE_WEB_FETCH` | bool | no | behaviour | Set to 1 to turn off the WebFetch tool |
| `claude_code_disable_windows_shell_launcher` | `CLAUDE_CODE_DISABLE_WINDOWS_SHELL_LAUNCHER` | bool | no | behaviour | Set to 1 to start PowerShell tool commands on Windows directly instead of through the cmd.exe launcher |
| `claude_code_disable_workflows` | `CLAUDE_CODE_DISABLE_WORKFLOWS` | bool | no | behaviour | Set to 1 to disable workflows |
| `claude_code_effort_level` | `CLAUDE_CODE_EFFORT_LEVEL` | str | no | model | Set the effort level for supported models |
| `claude_code_emit_session_state_events` | `CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS` | bool | no | behaviour | Set to 1 to add session_state_changed messages, which carry the session's state, to the message stream |
| `claude_code_enable_auto_mode` | `CLAUDE_CODE_ENABLE_AUTO_MODE` | str | no | behaviour | Accepted for compatibility with older releases and has no effect |
| `claude_code_enable_away_summary` | `CLAUDE_CODE_ENABLE_AWAY_SUMMARY` | str | no | behaviour | Override session recap availability |
| `claude_code_enable_background_plugin_refresh` | `CLAUDE_CODE_ENABLE_BACKGROUND_PLUGIN_REFRESH` | bool | no | plugins | Set to 1 to refresh plugin state at turn boundaries in non-interactive mode after a background install completes |
| `claude_code_enable_cfc` | `CLAUDE_CODE_ENABLE_CFC` | bool | no | behaviour | Set to 1 to start a CLI session with Chrome integration on, or 0 to start with it off |
| `claude_code_enable_feedback_survey_for_otel` | `CLAUDE_CODE_ENABLE_FEEDBACK_SURVEY_FOR_OTEL` | bool | no | telemetry | Set to 1 to route the "How is Claude doing?" session quality survey to your own OpenTelemetry collector when Anthropi... |
| `claude_code_enable_fine_grained_tool_streaming` | `CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING` | bool | no | behaviour | Controls whether tool call inputs stream from the API as Claude generates them |
| `claude_code_enable_gateway_model_discovery` | `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY` | bool | no | model | Set to 1 to populate the /model picker from your gateway's /v1/models endpoint when ANTHROPIC_BASE_URL points at an A... |
| `claude_code_enable_opus_4_7_fast_mode` | `CLAUDE_CODE_ENABLE_OPUS_4_7_FAST_MODE` | str | no | model | Removed in v2.1.142, when the fast mode default moved from Opus 4.6 to Opus 4.7 |
| `claude_code_enable_prompt_suggestion` | `CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION` | bool | no | behaviour | Set to false to turn off prompt suggestions, the grayed-out predictions that appear in your prompt input |
| `claude_code_enable_tasks` | `CLAUDE_CODE_ENABLE_TASKS` | str | no | behaviour | Selects which task-tracking tools Claude Code provides in sessions that have them |
| `claude_code_enable_telemetry` | `CLAUDE_CODE_ENABLE_TELEMETRY` | bool | no | telemetry | Set to 1 to enable OpenTelemetry data collection for metrics and logging |
| `claude_code_enable_todo_tools` | `CLAUDE_CODE_ENABLE_TODO_TOOLS` | bool | no | behaviour | Set to 1 to get the task-tracking tools on every model |
| `claude_code_exit_after_stop_delay` | `CLAUDE_CODE_EXIT_AFTER_STOP_DELAY` | int | no | behaviour | Time in milliseconds to wait after the query loop becomes idle before automatically exiting |
| `claude_code_experimental_agent_teams` | `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` | bool | no | behaviour | Set to 1 to enable agent teams |
| `claude_code_extra_body` | `CLAUDE_CODE_EXTRA_BODY` | str | no | behaviour | JSON object to merge into the top level of every API request body |
| `claude_code_file_read_max_output_tokens` | `CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS` | int | no | model | Override the default token limit for file reads, which is 25,000 tokens |
| `claude_code_force_session_persistence` | `CLAUDE_CODE_FORCE_SESSION_PERSISTENCE` | bool | no | behaviour | Set to 1 to force transcript persistence, prompt history, and claude agents registration even when this claude was la... |
| `claude_code_force_strikethrough` | `CLAUDE_CODE_FORCE_STRIKETHROUGH` | bool | no | behaviour | Set to 1 to force strikethrough rendering for ~~text~~ in Claude's responses when your terminal supports it but is no... |
| `claude_code_force_sync_output` | `CLAUDE_CODE_FORCE_SYNC_OUTPUT` | bool | no | behaviour | Set to 1 to force-enable DEC private mode 2026 synchronized output when your terminal supports it but is not auto-det... |
| `claude_code_force_terminal_images` | `CLAUDE_CODE_FORCE_TERMINAL_IMAGES` | bool | no | behaviour | Set to 1 to draw mod Image elements as pictures when your terminal draws kitty graphics protocol images with Unicode ... |
| `claude_code_fork_subagent` | `CLAUDE_CODE_FORK_SUBAGENT` | str | no | behaviour | Controls fork mode, which is on by default and lets Claude start forks on its own |
| `claude_code_forward_subagent_text` | `CLAUDE_CODE_FORWARD_SUBAGENT_TEXT` | bool | no | behaviour | Set to 1 to emit subagent text and thinking blocks in claude -p --output-format stream-json output, the same behavior... |
| `claude_code_gateway_hint_headers` | `CLAUDE_CODE_GATEWAY_HINT_HEADERS` | bool | no | behaviour | Set to 1 to send the gateway hint headers, such as x-claude-code-request-class and x-claude-code-compaction, on a cus... |
| `claude_code_gateway_model_discovery_timeout_ms` | `CLAUDE_CODE_GATEWAY_MODEL_DISCOVERY_TIMEOUT_MS` | int | no | model | Timeout in milliseconds for the gateway model discovery request that CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY turns... |
| `claude_code_git_bash_path` | `CLAUDE_CODE_GIT_BASH_PATH` | str | no | behaviour | Windows only: path to the Git Bash executable (bash.exe) |
| `claude_code_glob_hidden` | `CLAUDE_CODE_GLOB_HIDDEN` | str | no | behaviour | Set to false to exclude dotfiles from results when Claude invokes the Glob tool |
| `claude_code_glob_no_ignore` | `CLAUDE_CODE_GLOB_NO_IGNORE` | str | no | behaviour | Set to false to make the Glob tool respect .gitignore patterns |
| `claude_code_glob_timeout_seconds` | `CLAUDE_CODE_GLOB_TIMEOUT_SECONDS` | int | no | behaviour | Timeout in seconds for Glob tool file discovery |
| `claude_code_goal_checkin_minutes` | `CLAUDE_CODE_GOAL_CHECKIN_MINUTES` | int | no | behaviour | How many minutes background work can keep an active goal waiting before Claude Code asks Claude to check on it |
| `claude_code_gzip_request_bodies` | `CLAUDE_CODE_GZIP_REQUEST_BODIES` | str | no | behaviour | Set to 0 to turn off gzip compression of Claude API, telemetry, and artifact publish request bodies sent to api.anthr... |
| `claude_code_hide_cwd` | `CLAUDE_CODE_HIDE_CWD` | bool | no | behaviour | Set to 1 to hide the working directory in the startup logo |
| `claude_code_ide_host_override` | `CLAUDE_CODE_IDE_HOST_OVERRIDE` | str | no | behaviour | Override the host address used to connect to the IDE extension |
| `claude_code_ide_skip_auto_install` | `CLAUDE_CODE_IDE_SKIP_AUTO_INSTALL` | bool | no | behaviour | Set to 1 to skip auto-installation of IDE extensions |
| `claude_code_ide_skip_valid_check` | `CLAUDE_CODE_IDE_SKIP_VALID_CHECK` | bool | no | behaviour | Set to 1 to skip validation of IDE lockfile entries during connection |
| `claude_code_idle_compact_min_tokens` | `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS` | int | no | model | Context size, in tokens, at which idle compaction applies to a conversation (default: 200000) |
| `claude_code_max_concurrent_subagents` | `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` | int | no | behaviour | How many subagents can be running in one session before the Agent tool refuses to spawn another (default: 20) |
| `claude_code_max_context_tokens` | `CLAUDE_CODE_MAX_CONTEXT_TOKENS` | int | no | model | Override the context window size Claude Code assumes for the active model |
| `claude_code_max_mcp_description_length` | `CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH` | int | no | mcp | Maximum length in characters of MCP tool descriptions and MCP server instructions that Claude Code sends to the model |
| `claude_code_max_output_tokens` | `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | int | no | model | Set the maximum number of output tokens for most requests |
| `claude_code_max_retries` | `CLAUDE_CODE_MAX_RETRIES` | int | no | behaviour | Override the number of times to retry failed API requests (default: 10) |
| `claude_code_max_subagents_per_session` | `CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION` | str | no | behaviour | Removed in v2.1.224 and now a no-op |
| `claude_code_max_subagent_spawn_depth` | `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` | int | no | behaviour | Number of subagent layers allowed below the main conversation (default: 3) |
| `claude_code_max_tool_use_concurrency` | `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY` | int | no | provider | Maximum number of read-only tools and subagents that can execute in parallel (default: 10) |
| `claude_code_max_turns` | `CLAUDE_CODE_MAX_TURNS` | int | no | behaviour | Cap the number of agentic turns when no explicit limit is passed |
| `claude_code_max_web_searches_per_session` | `CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION` | int | no | behaviour | Cap on WebSearch calls (default: 200) |
| `claude_code_mcp_allowlist_env` | `CLAUDE_CODE_MCP_ALLOWLIST_ENV` | bool | no | mcp | Set to 1 to spawn stdio MCP servers with only a safe baseline environment plus the server's configured env, instead o... |
| `claude_code_mcp_auto_background_ms` | `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS` | int | no | mcp | Elapsed time in milliseconds before a still-running MCP tool call moves to a background task (default: 120000, or 2 m... |
| `claude_code_mcp_startup_wait_ms` | `CLAUDE_CODE_MCP_STARTUP_WAIT_MS` | int | no | mcp | How long in milliseconds the first turn of a non-interactive session waits for MCP servers that are still connecting,... |
| `claude_code_mcp_tool_idle_timeout` | `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` | int | no | mcp | Idle timeout in milliseconds for MCP tool calls |
| `claude_code_messaging_socket` | `CLAUDE_CODE_MESSAGING_SOCKET` | str | no | behaviour | Set by Claude Code, not by you: in sessions that bind an inbox socket, Claude Code exports that socket's path to hook... |
| `claude_code_messaging_token` | `CLAUDE_CODE_MESSAGING_TOKEN` | secret | yes | auth | Set by Claude Code, not by you: in sessions that bind an inbox socket, Claude Code exports this per-session token to ... |
| `claude_code_native_cursor` | `CLAUDE_CODE_NATIVE_CURSOR` | bool | no | behaviour | Set to 1 to show the terminal's own cursor at the input caret instead of a drawn block |
| `claude_code_new_init` | `CLAUDE_CODE_NEW_INIT` | bool | no | behaviour | Set to 1 to make /init run an interactive setup flow |
| `claude_code_nonblocking_stdout` | `CLAUDE_CODE_NONBLOCKING_STDOUT` | bool | no | behaviour | Set to 1 to write terminal output through a second non-blocking file descriptor, so a terminal that stops reading, su... |
| `claude_code_nonstreaming_timeout_retries` | `CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES` | int | no | behaviour | Limit how many times Claude Code re-sends a non-streaming request that times out |
| `claude_code_no_flicker` | `CLAUDE_CODE_NO_FLICKER` | bool | no | behaviour | Set to 1 to enable fullscreen rendering, a research preview that reduces flicker and keeps memory flat in long conver... |
| `claude_code_oauth_refresh_token` | `CLAUDE_CODE_OAUTH_REFRESH_TOKEN` | secret | yes | auth | OAuth refresh token for Claude.ai authentication |
| `claude_code_oauth_scopes` | `CLAUDE_CODE_OAUTH_SCOPES` | str | no | auth | Space-separated OAuth scopes the refresh token was issued with, such as "user:profile user:inference user:sessions:cl... |
| `claude_code_oauth_token` | `CLAUDE_CODE_OAUTH_TOKEN` | secret | yes | auth | OAuth access token for claude.ai authentication |
| `claude_code_opus_4_6_fast_mode_override` | `CLAUDE_CODE_OPUS_4_6_FAST_MODE_OVERRIDE` | str | no | model | Removed in v2.1.160 and now a no-op |
| `claude_code_otel_content_max_length` | `CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH` | int | no | telemetry | Maximum length of content-bearing OpenTelemetry attributes (model responses, tool content, system prompts, raw API bo... |
| `claude_code_otel_diag_stderr` | `CLAUDE_CODE_OTEL_DIAG_STDERR` | bool | no | telemetry | Set to 1 to write OpenTelemetry exporter diagnostic errors to stderr |
| `claude_code_otel_flush_timeout_ms` | `CLAUDE_CODE_OTEL_FLUSH_TIMEOUT_MS` | int | no | telemetry | Timeout in milliseconds for flushing pending OpenTelemetry spans (default: 5000) |
| `claude_code_otel_headers_helper_debounce_ms` | `CLAUDE_CODE_OTEL_HEADERS_HELPER_DEBOUNCE_MS` | int | no | telemetry | Interval for refreshing dynamic OpenTelemetry headers in milliseconds (default: 1740000 / 29 minutes) |
| `claude_code_otel_shutdown_timeout_ms` | `CLAUDE_CODE_OTEL_SHUTDOWN_TIMEOUT_MS` | int | no | telemetry | Timeout in milliseconds for the OpenTelemetry exporter to finish on shutdown (default: 2000) |
| `claude_code_overloaded_retry_base_delay_ms` | `CLAUDE_CODE_OVERLOADED_RETRY_BASE_DELAY_MS` | int | no | behaviour | Starting delay in milliseconds, in place of the default 500, of the exponential backoff between automatic retries of ... |
| `claude_code_overloaded_retry_max_delay_ms` | `CLAUDE_CODE_OVERLOADED_RETRY_MAX_DELAY_MS` | int | no | behaviour | Ceiling in milliseconds, in place of the default 32000, on the exponential backoff between automatic retries of a req... |
| `claude_code_package_manager_auto_update` | `CLAUDE_CODE_PACKAGE_MANAGER_AUTO_UPDATE` | bool | no | behaviour | Set to 1 to let Claude Code run your package manager's upgrade command in the background when a new version is available |
| `claude_code_perforce_mode` | `CLAUDE_CODE_PERFORCE_MODE` | bool | no | behaviour | Set to 1 to enable Perforce-aware write protection |
| `claude_code_plugin_cache_dir` | `CLAUDE_CODE_PLUGIN_CACHE_DIR` | str | no | plugins | Override the plugins root directory |
| `claude_code_plugin_dirs` | `CLAUDE_CODE_PLUGIN_DIRS` | str | no | plugins | Plugin directories to load for the session, each loaded the way a --plugin-dir flag loads it |
| `claude_code_plugin_dir_watch` | `CLAUDE_CODE_PLUGIN_DIR_WATCH` | bool | no | plugins | Controls whether Claude Code reloads a mod when the mod's files change |
| `claude_code_plugin_git_timeout_ms` | `CLAUDE_CODE_PLUGIN_GIT_TIMEOUT_MS` | int | no | plugins | Timeout in milliseconds for cloning or refreshing a plugin marketplace (default: 120000) |
| `claude_code_plugin_keep_marketplace_on_failure` | `CLAUDE_CODE_PLUGIN_KEEP_MARKETPLACE_ON_FAILURE` | bool | no | plugins | Set to 1 to skip the re-clone attempt and keep using the existing marketplace checkout when a marketplace refresh can... |
| `claude_code_plugin_prefer_https` | `CLAUDE_CODE_PLUGIN_PREFER_HTTPS` | bool | no | plugins | Set to 1 to clone GitHub owner/repo shorthand sources over HTTPS instead of SSH. Applies to plugin install and update... |
| `claude_code_plugin_seed_dir` | `CLAUDE_CODE_PLUGIN_SEED_DIR` | str | no | plugins | Path to one or more read-only plugin seed directories, separated by : on Unix or ; on Windows |
| `claude_code_powershell_respect_execution_policy` | `CLAUDE_CODE_POWERSHELL_RESPECT_EXECUTION_POLICY` | bool | no | behaviour | Set to 1 to stop Claude Code from passing -ExecutionPolicy Bypass when spawning PowerShell for tool calls, hooks, and... |
| `claude_code_print_bg_wait_ceiling_ms` | `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` | int | no | behaviour | Ceiling in milliseconds on idle waiting for background work, such as subagents and workflows, after the final turn in... |
| `claude_code_process_wrapper` | `CLAUDE_CODE_PROCESS_WRAPPER` | str | no | behaviour | Launch the processes Claude Code starts from its own binary, such as the background service that hosts agent view ses... |
| `claude_code_project_dir_name` | `CLAUDE_CODE_PROJECT_DIR_NAME` | str | no | behaviour | Set together with CLAUDE_CONFIG_DIR to choose the projects/ directory name Claude Code stores that session's transcri... |
| `claude_code_prompt_cache_ttl` | `CLAUDE_CODE_PROMPT_CACHE_TTL` | str | no | model | Set 5m or 1h, the only values Claude Code accepts, to choose the prompt cache TTL for the main conversation: your int... |
| `claude_code_propagate_traceparent` | `CLAUDE_CODE_PROPAGATE_TRACEPARENT` | bool | no | telemetry | Set to 1 to propagate W3C trace context when ANTHROPIC_BASE_URL points at a custom proxy |
| `claude_code_provider_managed_by_host` | `CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST` | str | no | behaviour | Set by host platforms that embed Claude Code and manage model provider routing on its behalf |
| `claude_code_proxy_resolves_hosts` | `CLAUDE_CODE_PROXY_RESOLVES_HOSTS` | bool | no | network | Set to 1 to allow the proxy to perform DNS resolution instead of the caller |
| `claude_code_remote` | `CLAUDE_CODE_REMOTE` | str | no | behaviour | Set automatically to true when Claude Code is running as a cloud session |
| `claude_code_remote_session_id` | `CLAUDE_CODE_REMOTE_SESSION_ID` | str | no | behaviour | Set automatically in cloud sessions to the current session's ID. Read this to construct a link back to the session tr... |
| `claude_code_restricted` | `CLAUDE_CODE_RESTRICTED` | bool | no | behaviour | Set to 1 to start the session in restricted mode, the same as passing --restricted |
| `claude_code_resume_interrupted_turn` | `CLAUDE_CODE_RESUME_INTERRUPTED_TURN` | bool | no | behaviour | Set to 1 to automatically resume if the previous session ended mid-turn |
| `claude_code_resume_interrupted_turn_max_age_ms` | `CLAUDE_CODE_RESUME_INTERRUPTED_TURN_MAX_AGE_MS` | int | no | behaviour | Maximum age in milliseconds of the last transcript message for a session that ended mid-turn to continue automaticall... |
| `claude_code_resume_prompt` | `CLAUDE_CODE_RESUME_PROMPT` | str | no | behaviour | Override the continuation message Claude Code sends to Claude when CLAUDE_CODE_RESUME_INTERRUPTED_TURN continues an i... |
| `claude_code_retry_watchdog` | `CLAUDE_CODE_RETRY_WATCHDOG` | bool | no | behaviour | Set to 1 for unattended sessions such as eval harnesses, CI jobs, or remote workers |
| `claude_code_retry_watchdog_max_wait_ms` | `CLAUDE_CODE_RETRY_WATCHDOG_MAX_WAIT_MS` | int | no | behaviour | Maximum time in milliseconds that each API request spends waiting out 429 and 529 errors when CLAUDE_CODE_RETRY_WATCH... |
| `claude_code_safe_mode` | `CLAUDE_CODE_SAFE_MODE` | bool | no | behaviour | Set to 1 to start in safe mode: CLAUDE.md, skills, plugins, hooks, MCP servers, custom commands and agents, output st... |
| `claude_code_script_caps` | `CLAUDE_CODE_SCRIPT_CAPS` | str | no | behaviour | JSON object limiting how many times specific scripts may be invoked per session when CLAUDE_CODE_SUBPROCESS_ENV_SCRUB... |
| `claude_code_scroll_speed` | `CLAUDE_CODE_SCROLL_SPEED` | str | no | behaviour | Set the mouse wheel scroll multiplier in fullscreen rendering |
| `claude_code_send_feedback` | `CLAUDE_CODE_SEND_FEEDBACK` | bool | no | privacy | Set to 0 to turn off Claude-drafted feedback for a session |
| `claude_code_sessionend_hooks_timeout_ms` | `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` | int | no | behaviour | Override the time budget in milliseconds for SessionEnd hooks |
| `claude_code_session_id` | `CLAUDE_CODE_SESSION_ID` | str | no | behaviour | Set automatically to the current session ID in Bash and PowerShell tool subprocesses, hook command subprocesses, and ... |
| `claude_code_shell` | `CLAUDE_CODE_SHELL` | str | no | behaviour | Set the shell Claude Code uses to run Bash tool commands |
| `claude_code_shell_prefix` | `CLAUDE_CODE_SHELL_PREFIX` | str | no | behaviour | Command prefix that wraps shell commands Claude Code spawns: Bash tool calls, hook commands, status line commands, an... |
| `claude_code_simple` | `CLAUDE_CODE_SIMPLE` | bool | no | behaviour | Set to 1 to run with a minimal system prompt and only the Bash, file read, and file edit tools |
| `claude_code_simple_system_prompt` | `CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT` | bool | no | behaviour | Choose between Claude Code's full system prompt and a shorter one with abbreviated tool descriptions |
| `claude_code_skip_anthropic_aws_auth` | `CLAUDE_CODE_SKIP_ANTHROPIC_AWS_AUTH` | bool | no | provider | Skip client-side authentication for Claude Platform on AWS, for gateways that sign requests themselves |
| `claude_code_skip_aws_cred_cache` | `CLAUDE_CODE_SKIP_AWS_CRED_CACHE` | bool | no | model | Set to 1 to turn off the in-process cache of credentials resolved from the AWS default credential provider chain, so ... |
| `claude_code_skip_bedrock_auth` | `CLAUDE_CODE_SKIP_BEDROCK_AUTH` | bool | no | provider | Skip AWS authentication for Amazon Bedrock (for example, when using an LLM gateway) |
| `claude_code_skip_fast_mode_network_errors` | `CLAUDE_CODE_SKIP_FAST_MODE_NETWORK_ERRORS` | bool | no | network | Set to 1 to treat a failed fast mode availability check as available, for networks that block the check's direct requ... |
| `claude_code_skip_fast_mode_org_check` | `CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK` | bool | no | model | Set to 1 to skip the client-side fast mode availability check, for proxies that intercept the check's request rather ... |
| `claude_code_skip_foundry_auth` | `CLAUDE_CODE_SKIP_FOUNDRY_AUTH` | bool | no | provider | Skip Azure authentication for Microsoft Foundry, for a proxy or gateway that injects its own Authorization header |
| `claude_code_skip_mantle_auth` | `CLAUDE_CODE_SKIP_MANTLE_AUTH` | bool | no | behaviour | Skip AWS authentication for Amazon Bedrock Mantle (for example, when using an LLM gateway) |
| `claude_code_skip_model_access_memory` | `CLAUDE_CODE_SKIP_MODEL_ACCESS_MEMORY` | str | no | model | The startup model checks on Amazon Bedrock and Google Cloud's Agent Platform remember on this machine which models th... |
| `claude_code_skip_prompt_history` | `CLAUDE_CODE_SKIP_PROMPT_HISTORY` | bool | no | privacy | Set to 1 to skip writing prompt history and session transcripts to disk |
| `claude_code_skip_vertex_auth` | `CLAUDE_CODE_SKIP_VERTEX_AUTH` | bool | no | provider | Skip Google authentication for Google Cloud's Agent Platform (for example, when using an LLM gateway) |
| `claude_code_startup_failure_results` | `CLAUDE_CODE_STARTUP_FAILURE_RESULTS` | bool | no | behaviour | Set to 1 to have a session started with --output-format stream-json write a result message naming why Claude Code ref... |
| `claude_code_stop_hook_block_cap` | `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` | str | no | behaviour | Maximum number of consecutive times a Stop or SubagentStop hook may block the turn from ending before Claude Code ove... |
| `claude_code_subagent_model` | `CLAUDE_CODE_SUBAGENT_MODEL` | str | no | model | The default model for subagents, agent team teammates, and workflow agents that aren't assigned a model another way |
| `claude_code_subagent_model_force` | `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` | bool | no | model | Set to 1 to force one model onto subagents, teammates, and workflow agents |
| `claude_code_subagent_prompt_cache_ttl` | `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL` | str | no | model | Set 5m or 1h, the only values Claude Code accepts, to choose the prompt cache TTL for requests outside the main conve... |
| `claude_code_subprocess_env_scrub` | `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` | bool | no | behaviour | Set to 1 to strip credentials from the environments of the subprocesses Claude Code starts, such as Bash commands, ho... |
| `claude_code_sync_plugin_install` | `CLAUDE_CODE_SYNC_PLUGIN_INSTALL` | bool | no | plugins | Set to 1 in non-interactive mode (the -p flag) to wait for plugin installation to complete before the first query |
| `claude_code_sync_plugin_install_timeout_ms` | `CLAUDE_CODE_SYNC_PLUGIN_INSTALL_TIMEOUT_MS` | int | no | plugins | Timeout in milliseconds for synchronous plugin installation |
| `claude_code_sync_skills` | `CLAUDE_CODE_SYNC_SKILLS` | bool | no | behaviour | Set to 1 in non-interactive mode with the -p flag to make Claude Code download the skills enabled for your claude.ai ... |
| `claude_code_sync_skills_install_timeout_ms` | `CLAUDE_CODE_SYNC_SKILLS_INSTALL_TIMEOUT_MS` | int | no | behaviour | Timeout in milliseconds for the skills resync that runs mid-session when an app built on the Agent SDK reloads skills... |
| `claude_code_sync_skills_wait_timeout_ms` | `CLAUDE_CODE_SYNC_SKILLS_WAIT_TIMEOUT_MS` | int | no | behaviour | Timeout in milliseconds for the first query to wait for the initial skill list when CLAUDE_CODE_SYNC_SKILLS is set (d... |
| `claude_code_syntax_highlight` | `CLAUDE_CODE_SYNTAX_HIGHLIGHT` | str | no | behaviour | Set to false to disable syntax highlighting in diff output |
| `claude_code_task_list_id` | `CLAUDE_CODE_TASK_LIST_ID` | str | no | behaviour | Share a task list across sessions |
| `claude_code_team_teardown_park_timeout_ms` | `CLAUDE_CODE_TEAM_TEARDOWN_PARK_TIMEOUT_MS` | int | no | behaviour | Override, in milliseconds, how long a non-interactive session waits at exit for its agent team to finish tearing down |
| `claude_code_tmpdir` | `CLAUDE_CODE_TMPDIR` | str | no | behaviour | Override the temp directory used for internal temp files |
| `claude_code_tmux_truecolor` | `CLAUDE_CODE_TMUX_TRUECOLOR` | str | no | behaviour | Set to any non-empty value, such as 1, to allow 24-bit truecolor output inside tmux |
| `claude_code_tool_memory_cgroup_exclude` | `CLAUDE_CODE_TOOL_MEMORY_CGROUP_EXCLUDE` | str | no | privacy | On Linux and WSL, set to a comma-separated list of the kinds of processes Claude Code excludes from the tool memory c... |
| `claude_code_tool_memory_limit` | `CLAUDE_CODE_TOOL_MEMORY_LIMIT` | str | no | privacy | On Linux and WSL, set to a size such as 4G to cap the memory that Bash and PowerShell tool commands can use, and Moni... |
| `claude_code_transcript_local_gc` | `CLAUDE_CODE_TRANSCRIPT_LOCAL_GC` | bool | no | behaviour | Set to 1 to limit how large the transcript file of a long -p or Agent SDK session grows |
| `claude_code_user_dialog_timeout_ms` | `CLAUDE_CODE_USER_DIALOG_TIMEOUT_MS` | int | no | behaviour | Deadline in milliseconds before Claude Code cancels a dialog it forwards to a remote client such as a Remote Control ... |
| `claude_code_use_anthropic_aws` | `CLAUDE_CODE_USE_ANTHROPIC_AWS` | bool | no | provider | Use Claude Platform on AWS |
| `claude_code_use_bedrock` | `CLAUDE_CODE_USE_BEDROCK` | bool | no | provider | Use Amazon Bedrock |
| `claude_code_use_foundry` | `CLAUDE_CODE_USE_FOUNDRY` | bool | no | provider | Use Microsoft Foundry |
| `claude_code_use_mantle` | `CLAUDE_CODE_USE_MANTLE` | bool | no | provider | Use the Amazon Bedrock Mantle endpoint |
| `claude_code_use_native_file_search` | `CLAUDE_CODE_USE_NATIVE_FILE_SEARCH` | bool | no | provider | Set to 1 to discover custom commands, subagents, and output styles using Node.js file APIs instead of ripgrep |
| `claude_code_use_powershell_tool` | `CLAUDE_CODE_USE_POWERSHELL_TOOL` | bool | no | provider | Controls the PowerShell tool |
| `claude_code_use_vertex` | `CLAUDE_CODE_USE_VERTEX` | bool | no | provider | Use Google Cloud's Agent Platform |
| `claude_code_webfetch_cache_ttl_ms` | `CLAUDE_CODE_WEBFETCH_CACHE_TTL_MS` | int | no | model | Set to the number of milliseconds WebFetch keeps each fetched URL's response cached |
| `claude_code_webfetch_deadline_ms` | `CLAUDE_CODE_WEBFETCH_DEADLINE_MS` | int | no | behaviour | Upper bound in milliseconds on how long WebFetch waits for a page to download, including any redirects it follows |
| `claude_code_web_search_refills_per_hour` | `CLAUDE_CODE_WEB_SEARCH_REFILLS_PER_HOUR` | str | no | behaviour | Rate at which a session's WebSearch limit refills, in calls per hour |
| `claude_code_worker_checkin_schedule` | `CLAUDE_CODE_WORKER_CHECKIN_SCHEDULE` | bool | no | behaviour | When CLAUDE_AUTO_BACKGROUND_TASKS is set to 1, how long Claude Code waits before each reminder to Claude to check on ... |
| `claude_code_workflow_max_concurrent_agents` | `CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS` | int | no | behaviour | How many agents a single workflow run executes at once, from 1 to 256. By default, a run executes up to 16 agents at ... |
| `claude_code_workflow_prefix_stagger_ms` | `CLAUDE_CODE_WORKFLOW_PREFIX_STAGGER_MS` | int | no | behaviour | Upper bound in milliseconds on how long a workflow agent waits for a same-prefix sibling's first response to begin be... |
| `claude_code_you_should_know_debug` | `CLAUDE_CODE_YOU_SHOULD_KNOW_DEBUG` | bool | no | behaviour | Set to 1 to show, above the prompt, a line for each of the last three checks the You should know mod made in this ses... |
| `claude_config_dir` | `CLAUDE_CONFIG_DIR` | str | no | behaviour | Override the configuration directory (default: ~/.claude) (managed by the service; cannot be set) |
| `claude_disable_adopt` | `CLAUDE_DISABLE_ADOPT` | bool | no | behaviour | Set to 1 to stop in-flight background work instead of carrying it over when you background a session by pressing  or ... |
| `claude_effort` | `CLAUDE_EFFORT` | str | no | model | Set automatically in Bash tool subprocesses and hook commands to the effort level in effect when the subprocess start... |
| `claude_enable_byte_watchdog` | `CLAUDE_ENABLE_BYTE_WATCHDOG` | bool | no | behaviour | Set to 1 to force-enable the byte-level streaming idle watchdog, or set to 0 to force-disable it |
| `claude_enable_byte_watchdog_bedrock` | `CLAUDE_ENABLE_BYTE_WATCHDOG_BEDROCK` | bool | no | provider | Set to 1 to enable the byte-level streaming idle watchdog on Amazon Bedrock vnd.amazon.eventstream responses, which a... |
| `claude_enable_stream_watchdog` | `CLAUDE_ENABLE_STREAM_WATCHDOG` | str | no | behaviour | Set to 0 to force-disable the event-level streaming idle watchdog, or set to 1 to force-enable it |
| `claude_env_file` | `CLAUDE_ENV_FILE` | str | no | behaviour | Path to a shell script whose contents Claude Code runs before each Bash command in the same shell process, so exports... |
| `claude_job_dir` | `CLAUDE_JOB_DIR` | str | no | behaviour | Set by Claude Code in each background session to that session's ~/.claude/jobs/<id> directory |
| `claude_pid` | `CLAUDE_PID` | str | no | behaviour | Claude Code sets this to its own process ID in the subprocesses it spawns: Bash and PowerShell tool commands and hook... |
| `claude_remote_control_session_name_prefix` | `CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX` | str | no | behaviour | Prefix for auto-generated Remote Control session names when no explicit name is provided |
| `claude_stream_first_byte_timeout_ms` | `CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS` | int | no | behaviour | Deadline in milliseconds for the first response byte of a streaming request, on the connections where the first-byte ... |
| `claude_stream_idle_timeout_ms` | `CLAUDE_STREAM_IDLE_TIMEOUT_MS` | int | no | behaviour | Timeout in milliseconds before the event- and byte-level streaming idle watchdogs close a stalled connection |
| `claude_subagent_bg_shell_max_ms` | `CLAUDE_SUBAGENT_BG_SHELL_MAX_MS` | str | no | behaviour | Removed in v2.1.260 and now a no-op |
| `cloud_ml_region` | `CLOUD_ML_REGION` | str | no | provider | Google Cloud region for Vertex AI |
| `debug` | `DEBUG` | bool | no | behaviour | Set to 1 to enable debug mode, equivalent to launching with --debug |
| `disable_autoupdater` | `DISABLE_AUTOUPDATER` | bool | no | privacy | Set to 1 to disable automatic background updates |
| `disable_auto_compact` | `DISABLE_AUTO_COMPACT` | bool | no | model | Set to 1 to disable automatic compaction when approaching the context limit, along with idle compaction |
| `disable_compact` | `DISABLE_COMPACT` | bool | no | model | Set to 1 to disable all compaction: both automatic compaction and the manual /compact command |
| `disable_cost_warnings` | `DISABLE_COST_WARNINGS` | bool | no | privacy | Set to 1 to disable cost warning messages |
| `disable_doctor_command` | `DISABLE_DOCTOR_COMMAND` | bool | no | behaviour | Set to 1 to hide the /doctor setup checkup skill and its /checkup alias |
| `disable_error_reporting` | `DISABLE_ERROR_REPORTING` | bool | no | privacy | Set to any non-empty value, such as 1, to opt out of error reporting |
| `disable_extra_usage_command` | `DISABLE_EXTRA_USAGE_COMMAND` | bool | no | behaviour | Set to 1 to hide the /usage-credits command that lets users purchase additional usage beyond rate limits |
| `disable_feedback_command` | `DISABLE_FEEDBACK_COMMAND` | bool | no | privacy | Set to 1 to disable the /feedback command and Claude-drafted feedback |
| `disable_growthbook` | `DISABLE_GROWTHBOOK` | bool | no | behaviour | Set to 1 or true to disable GrowthBook feature-flag fetching and use code defaults for every flag |
| `disable_installation_checks` | `DISABLE_INSTALLATION_CHECKS` | bool | no | behaviour | Set to 1 to disable installation warnings |
| `disable_install_github_app_command` | `DISABLE_INSTALL_GITHUB_APP_COMMAND` | bool | no | behaviour | Set to 1 to hide the /install-github-app command |
| `disable_interleaved_thinking` | `DISABLE_INTERLEAVED_THINKING` | bool | no | model | Set to 1 to prevent sending the interleaved-thinking beta header |
| `disable_login_command` | `DISABLE_LOGIN_COMMAND` | bool | no | auth | Set to 1 to hide the /login command |
| `disable_logout_command` | `DISABLE_LOGOUT_COMMAND` | bool | no | behaviour | Set to 1 to hide the /logout command |
| `disable_prompt_caching` | `DISABLE_PROMPT_CACHING` | bool | no | behaviour | Set to 1 to disable prompt caching for all models (takes precedence over per-model settings) |
| `disable_prompt_caching_fable` | `DISABLE_PROMPT_CACHING_FABLE` | bool | no | behaviour | Set to 1 to disable prompt caching for Fable models |
| `disable_prompt_caching_haiku` | `DISABLE_PROMPT_CACHING_HAIKU` | bool | no | behaviour | Set to 1 to disable prompt caching for the default Haiku model, wherever it runs |
| `disable_prompt_caching_opus` | `DISABLE_PROMPT_CACHING_OPUS` | bool | no | behaviour | Set to 1 to disable prompt caching for the default Opus model |
| `disable_prompt_caching_sonnet` | `DISABLE_PROMPT_CACHING_SONNET` | bool | no | behaviour | Set to 1 to disable prompt caching for the default Sonnet model |
| `disable_telemetry` | `DISABLE_TELEMETRY` | bool | no | telemetry | Set to any non-empty value, such as 1, to opt out of telemetry |
| `disable_updates` | `DISABLE_UPDATES` | bool | no | behaviour | Set to 1 to block all updates including manual claude update and claude install |
| `disable_upgrade_command` | `DISABLE_UPGRADE_COMMAND` | bool | no | behaviour | Set to 1 to hide the /upgrade command |
| `do_not_track` | `DO_NOT_TRACK` | bool | no | behaviour | Set to 1 to opt out of telemetry, with the same effect as DISABLE_TELEMETRY, including on feature-flag fetching |
| `enable_beta_tracing_detailed` | `ENABLE_BETA_TRACING_DETAILED` | bool | no | behaviour | Set to 1, and set BETA_TRACING_ENDPOINT to your OTLP/HTTP collector endpoint, to turn on detailed beta tracing, which... |
| `enable_claudeai_mcp_servers` | `ENABLE_CLAUDEAI_MCP_SERVERS` | str | no | mcp | Set to false to stop Claude Code from fetching claude.ai MCP servers |
| `enable_prompt_caching_1h` | `ENABLE_PROMPT_CACHING_1H` | bool | no | behaviour | Set to 1 to request a 1-hour prompt cache TTL instead of the default 5 minutes |
| `enable_prompt_caching_1h_bedrock` | `ENABLE_PROMPT_CACHING_1H_BEDROCK` | str | no | provider | Deprecated |
| `enable_tool_search` | `ENABLE_TOOL_SEARCH` | str | no | behaviour | Controls MCP tool search |
| `fallback_for_all_primary_models` | `FALLBACK_FOR_ALL_PRIMARY_MODELS` | str | no | model | Set to any non-empty value, such as 1, to make Claude Code stop retrying on repeated overload errors for every model ... |
| `force_autoupdate_plugins` | `FORCE_AUTOUPDATE_PLUGINS` | bool | no | plugins | Set to 1 to force plugin auto-updates even when the main auto-updater is disabled via DISABLE_AUTOUPDATER |
| `force_hyperlink` | `FORCE_HYPERLINK` | bool | no | behaviour | Set to 1 to enable clickable OSC 8 hyperlinks when your terminal supports them but isn't auto-detected, or 0 to disab... |
| `force_prompt_caching_5m` | `FORCE_PROMPT_CACHING_5M` | bool | no | behaviour | Set to 1 to force the 5-minute prompt cache TTL even when 1-hour TTL would otherwise apply |
| `https_proxy` | `HTTPS_PROXY` | str | no | network | Specify HTTPS proxy server for network connections |
| `http_proxy` | `HTTP_PROXY` | str | no | network | Specify HTTP proxy server for network connections |
| `is_demo` | `IS_DEMO` | str | no | behaviour | Set to any non-empty value, such as 1, to enable demo mode: hides your email and organization name from the header an... |
| `max_mcp_output_tokens` | `MAX_MCP_OUTPUT_TOKENS` | int | no | mcp | Maximum number of tokens allowed in MCP tool responses (default: 25000) |
| `max_structured_output_retries` | `MAX_STRUCTURED_OUTPUT_RETRIES` | int | no | behaviour | Number of attempts Claude Code allows when the model's response fails validation against the --json-schema in non-int... |
| `max_thinking_tokens` | `MAX_THINKING_TOKENS` | int | no | model | Fixed token budget for extended thinking |
| `mcp_client_secret` | `MCP_CLIENT_SECRET` | secret | yes | mcp | OAuth client secret for MCP servers that require pre-configured credentials |
| `mcp_connection_nonblocking` | `MCP_CONNECTION_NONBLOCKING` | str | no | mcp | Controls whether startup waits for MCP servers to connect before the first query |
| `mcp_connect_timeout_ms` | `MCP_CONNECT_TIMEOUT_MS` | int | no | mcp | How long blocking MCP startup waits, in milliseconds, for the connection batch before snapshotting the tool list (def... |
| `mcp_discovery_cache` | `MCP_DISCOVERY_CACHE` | bool | no | mcp | Turns the MCP discovery cache on or off |
| `mcp_discovery_cache_max_stale_s` | `MCP_DISCOVERY_CACHE_MAX_STALE_S` | int | no | mcp | Maximum age, in seconds, of a discovery-cache entry (default: 14400, or 4 hours) |
| `mcp_discovery_cache_strikes` | `MCP_DISCOVERY_CACHE_STRIKES` | str | no | mcp | At a start where a discovery-cache entry is older than MCP_DISCOVERY_CACHE_TTL_S, Claude Code refreshes it in the bac... |
| `mcp_discovery_cache_ttl_s` | `MCP_DISCOVERY_CACHE_TTL_S` | str | no | mcp | Seconds for which Claude Code uses a discovery-cache entry without refreshing it (default: 900) |
| `mcp_oauth_callback_port` | `MCP_OAUTH_CALLBACK_PORT` | int | no | auth | Fixed port for the OAuth redirect callback, as an alternative to --callback-port when adding an MCP server with pre-c... |
| `mcp_protocol_negotiation` | `MCP_PROTOCOL_NEGOTIATION` | str | no | mcp | On the v2 MCP client runtime only, whether Claude Code probes servers for MCP protocol revision 2026-07-28. Set auto ... |
| `mcp_remote_server_connection_batch_size` | `MCP_REMOTE_SERVER_CONNECTION_BATCH_SIZE` | int | no | mcp | Maximum number of remote MCP servers (HTTP/SSE) to connect in parallel during startup (default: 20) |
| `mcp_sdk_generation` | `MCP_SDK_GENERATION` | str | no | mcp | Pin which MCP client runtime this process connects to MCP servers with: v1, built on MCP TypeScript SDK 1.x, or v2, b... |
| `mcp_server_connection_batch_size` | `MCP_SERVER_CONNECTION_BATCH_SIZE` | int | no | mcp | Maximum number of local MCP servers (stdio) to connect in parallel during startup (default: 3) |
| `mcp_timeout` | `MCP_TIMEOUT` | int | no | mcp | Timeout in milliseconds for MCP server startup (default: 30000, or 30 seconds) |
| `mcp_tool_timeout` | `MCP_TOOL_TIMEOUT` | int | no | mcp | Timeout in milliseconds for MCP tool execution (default: 100000000, about 28 hours) |
| `no_proxy` | `NO_PROXY` | str | no | network | List of domains and IPs to which requests will be directly issued, bypassing proxy |
| `otel_attribute_value_length_limit` | `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT` | int | no | telemetry | Standard OpenTelemetry SDK limit on attribute value length |
| `otel_log_assistant_responses` | `OTEL_LOG_ASSISTANT_RESPONSES` | bool | no | telemetry | Set to 1 to include the model's response text on assistant_response OpenTelemetry log events |
| `otel_log_managed_settings` | `OTEL_LOG_MANAGED_SETTINGS` | bool | no | telemetry | Set to 1 to add the redacted managed settings, and a SHA-256 digest of the settings before redaction, to managed_sett... |
| `otel_log_raw_api_bodies` | `OTEL_LOG_RAW_API_BODIES` | bool | no | telemetry | Emit Anthropic Messages API request and response JSON as api_request_body / api_response_body log events |
| `otel_log_tool_content` | `OTEL_LOG_TOOL_CONTENT` | bool | no | telemetry | Set to 1 to include tool content in the tool.output OpenTelemetry span event |
| `otel_log_tool_details` | `OTEL_LOG_TOOL_DETAILS` | bool | no | telemetry | Set to 1 to include tool input arguments; MCP server names; user-authored workflow names; raw error strings on tool f... |
| `otel_log_user_prompts` | `OTEL_LOG_USER_PROMPTS` | bool | no | telemetry | Set to 1 to include user prompt text in OpenTelemetry traces and logs |
| `otel_metrics_include_account_uuid` | `OTEL_METRICS_INCLUDE_ACCOUNT_UUID` | str | no | telemetry | Set to false to exclude account UUID from metrics attributes (default: included) |
| `otel_metrics_include_entrypoint` | `OTEL_METRICS_INCLUDE_ENTRYPOINT` | bool | no | telemetry | Set to true to include the session entrypoint in metrics attributes (default: excluded) |
| `otel_metrics_include_repository` | `OTEL_METRICS_INCLUDE_REPOSITORY` | bool | no | telemetry | Set to true to tag OpenTelemetry metrics and events with vcs.* attributes identifying the session's repository (defau... |
| `otel_metrics_include_resource_attributes` | `OTEL_METRICS_INCLUDE_RESOURCE_ATTRIBUTES` | str | no | telemetry | As of v2.1.161, Claude Code attaches OTEL_RESOURCE_ATTRIBUTES keys to metric datapoint labels |
| `otel_metrics_include_session_id` | `OTEL_METRICS_INCLUDE_SESSION_ID` | str | no | telemetry | Set to false to exclude session ID from metrics attributes (default: included) |
| `otel_metrics_include_version` | `OTEL_METRICS_INCLUDE_VERSION` | bool | no | telemetry | Set to true to include Claude Code version in metrics attributes (default: excluded) |
| `slash_command_tool_char_budget` | `SLASH_COMMAND_TOOL_CHAR_BUDGET` | str | no | behaviour | Override the character budget for skill metadata shown to the Skill tool |
| `task_max_output_length` | `TASK_MAX_OUTPUT_LENGTH` | str | no | behaviour | Removed in v2.1.277 and now a no-op, together with the TaskOutput tool it sized |
| `use_builtin_ripgrep` | `USE_BUILTIN_RIPGREP` | str | no | provider | Set to 0 to use system-installed rg instead of rg included with Claude Code |
| `vertex_region_claude_3_5_haiku` | `VERTEX_REGION_CLAUDE_3_5_HAIKU` | str | no | provider | Override region for Claude 3.5 Haiku when using Google Cloud's Agent Platform |
| `vertex_region_claude_3_5_sonnet` | `VERTEX_REGION_CLAUDE_3_5_SONNET` | str | no | provider | Override region for Claude 3.5 Sonnet when using Google Cloud's Agent Platform |
| `vertex_region_claude_3_7_sonnet` | `VERTEX_REGION_CLAUDE_3_7_SONNET` | str | no | provider | Override region for Claude 3.7 Sonnet when using Google Cloud's Agent Platform |
| `vertex_region_claude_4_0_opus` | `VERTEX_REGION_CLAUDE_4_0_OPUS` | str | no | provider | Override region for Claude Opus 4 when using Google Cloud's Agent Platform |
| `vertex_region_claude_4_0_sonnet` | `VERTEX_REGION_CLAUDE_4_0_SONNET` | str | no | provider | Override region for Claude Sonnet 4 when using Google Cloud's Agent Platform |
| `vertex_region_claude_4_1_opus` | `VERTEX_REGION_CLAUDE_4_1_OPUS` | str | no | provider | Override region for Claude Opus 4.1 when using Google Cloud's Agent Platform |
| `vertex_region_claude_4_5_opus` | `VERTEX_REGION_CLAUDE_4_5_OPUS` | str | no | provider | Override region for Claude Opus 4.5 when using Google Cloud's Agent Platform |
| `vertex_region_claude_4_5_sonnet` | `VERTEX_REGION_CLAUDE_4_5_SONNET` | str | no | provider | Override region for Claude Sonnet 4.5 when using Google Cloud's Agent Platform |
| `vertex_region_claude_4_6_opus` | `VERTEX_REGION_CLAUDE_4_6_OPUS` | str | no | provider | Override region for Claude Opus 4.6 when using Google Cloud's Agent Platform |
| `vertex_region_claude_4_6_sonnet` | `VERTEX_REGION_CLAUDE_4_6_SONNET` | str | no | provider | Override region for Claude Sonnet 4.6 when using Google Cloud's Agent Platform |
| `vertex_region_claude_4_7_opus` | `VERTEX_REGION_CLAUDE_4_7_OPUS` | str | no | provider | Override region for Claude Opus 4.7 when using Google Cloud's Agent Platform |
| `vertex_region_claude_4_8_opus` | `VERTEX_REGION_CLAUDE_4_8_OPUS` | str | no | provider | Override region for Claude Opus 4.8 when using Google Cloud's Agent Platform |
| `vertex_region_claude_5_5_opus` | `VERTEX_REGION_CLAUDE_5_5_OPUS` | str | no | provider | Override region for Claude Opus 5.5 when using Google Cloud's Agent Platform |
| `vertex_region_claude_5_5_sonnet` | `VERTEX_REGION_CLAUDE_5_5_SONNET` | str | no | provider | Override region for Claude Sonnet 5.5 when using Google Cloud's Agent Platform |
| `vertex_region_claude_5_opus` | `VERTEX_REGION_CLAUDE_5_OPUS` | str | no | provider | Override region for Claude Opus 5 when using Google Cloud's Agent Platform |
| `vertex_region_claude_5_sonnet` | `VERTEX_REGION_CLAUDE_5_SONNET` | str | no | provider | Override region for Claude Sonnet 5 when using Google Cloud's Agent Platform |
| `vertex_region_claude_fable_5` | `VERTEX_REGION_CLAUDE_FABLE_5` | str | no | provider | Override region for Claude Fable 5 when using Google Cloud's Agent Platform |
| `vertex_region_claude_fable_5_1` | `VERTEX_REGION_CLAUDE_FABLE_5_1` | str | no | provider | Override region for Claude Fable 5.1 when using Google Cloud's Agent Platform |
| `vertex_region_claude_haiku_4_5` | `VERTEX_REGION_CLAUDE_HAIKU_4_5` | str | no | provider | Override region for Claude Haiku 4.5 when using Google Cloud's Agent Platform |
| `vertex_region_claude_haiku_5_5` | `VERTEX_REGION_CLAUDE_HAIKU_5_5` | str | no | provider | Override region for Claude Haiku 5.5 when using Google Cloud's Agent Platform |

Related, outside `claude.env`: `claude.passthrough_env` copies listed variables (for example
`GH_TOKEN`, `GITLAB_TOKEN`, `SSH_AUTH_SOCK`) from the service environment, and `TERM` is always
`xterm-256color`.

## Docker and compose

The `Dockerfile` is multi-stage: a builder resolves the locked Python dependencies with a pinned
`uv`, a pinned official Node image supplies `node`, and the runtime is `python:3.12-slim-bookworm`.

| Build arg or pin | Default | Purpose |
|---|---|---|
| `CLAUDE_CODE_VERSION` | `2.1.304` | `@anthropic-ai/claude-code` npm version; the PTY driver depends on its dialogs and hooks |
| `RUFF_VERSION` | `0.15.0` | ruff for agent sessions |
| `GH_VERSION` | `2.102.0` | GitHub CLI release binary |
| `GLAB_VERSION` | `1.124.0` | GitLab CLI release binary |
| uv, node, mise | `0.13.4`, `24.22.0`, `2026.10.15` | pinned in `FROM`/`COPY --from` lines |

Override a build arg with `docker compose build --build-arg CLAUDE_CODE_VERSION=...`.

- **Non-root user.** The service runs as `agent` (uid/gid 1000, home `/home/agent`); Claude
  refuses `bypassPermissions` as root.
- **Volumes.** `workdir` at `/workdir`, `sessions` at `/sessions`, `mise` at `/opt/mise`.
  - `workdir`: per-session workdirs (`/workdir/<name>`), or mounted trees.
  - `sessions`: `/sessions/<session_id>/` holding `session.json` (state, no secret values),
    `config/` (the session `CLAUDE_CONFIG_DIR`: `settings.json`, `.claude.json`, `CLAUDE.md`,
    plugins, `projects/<project>/<id>.jsonl` transcripts), `events.jsonl` (the event log),
    `turns.jsonl` (turn records), `gitconfig` and `setup.done`.
  - `DELETE /v1/sessions/{id}` keeps all of these files unless `purge=true` (a mounted workdir is never deleted).
  - `mise`: the mise data dir (`MISE_DATA_DIR`) with installed tools; shims are on `PATH`.
- **Config mount.** `${CCW_CONFIG_FILE:-./config.example.yaml}` is mounted read-only at
  `/etc/ccw/config.yaml`; `CCW_CONFIG` points there.
- **Env passthrough.** Compose forwards `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`,
  `CCW_AUTH_TOKEN`, `GH_TOKEN` and `GITLAB_TOKEN` from the host environment (empty when unset); the
  config file consumes them through `${VAR:-}`.
- **Ports.** `8080` is the public API (`${CCW_PORT:-8080}` on the host). The internal hook API on
  `127.0.0.1:8090` is loopback inside the container and is never published.
- **Healthcheck.** `curl -f http://127.0.0.1:8080/healthz` every 30s (1s during the start
  period); `ccw-ready` makes `docker compose up -d` return only once the service is healthy.
- **Shutdown.** `stop_grace_period: 30s` must exceed `limits.shutdown_timeout_seconds`.

**Anthropic policy note.** Anthropic's terms for Claude subscriptions (OAuth tokens from
`claude setup-token`) are for your own use of Claude Code. Products offered to third parties
should authenticate with an API key unless Anthropic has approved otherwise. The service supports
both and passes whichever you configure; choosing and complying is your responsibility.

## Running in Kubernetes

No chart is shipped; the image runs as-is.

- **Liveness**: `GET /healthz` returns `200` as soon as the process serves HTTP.
- **Readiness**: `GET /readyz` returns `503 not_ready` until config, global mise tooling and
  persisted-session loading finished, then `200`. Route traffic only on readiness.
- **Shutdown**: on SIGTERM the service stops accepting work and stops sessions within
  `limits.shutdown_timeout_seconds`; set `terminationGracePeriodSeconds` above it.
- **Logs**: JSON, one object per line, on stdout; level from `server.log_level`.
- **Config**: mount the YAML (ConfigMap) and set `CCW_CONFIG` to its path; inject secrets as env
  vars consumed through `${VAR}`.
- **Persistent storage**: back `paths.sessions_root` (sessions and transcripts, required to
  resume after a restart) and, if workdirs must survive, `paths.workdir_root`. The mise volume is a
  cache and may be ephemeral.
- **Job completion and termination message**: `sessions.exit_when_last_destroyed: true` makes the
  process exit 0 once the last session is deleted (or reaped idle), so a Job completes;
  `sessions.exit_when_dead: true` exits 3 when a session dies and writes the reason to
  `/dev/termination-log` for the pod's termination message. Both stop through the normal shutdown.
- **Replicas**: one instance per project or assignment; see the [Trust model](#trust-model). Keep
  the Service cluster-internal and never expose it through a public ingress.

## Workbench usage

The workbench plugin flow runs inside this service with `examples/workbench.config.yaml` and the
compose override `examples/docker-compose.workbench.yml`:

```bash
WORKBENCH_TREE=~/Documents/workbench \
  docker compose -f docker-compose.yml -f examples/docker-compose.workbench.yml up -d
```

The override mounts the plugin directory (`WORKBENCH_PLUGIN_DIR`, default
`~/Documents/claude-infra-plugin`) read-only at `/opt/plugins/claude-infra-plugin`, the workbench
tree at `/workdir/workbench`, `~/.ssh` read-only, and the example config; it forwards
`GIT_USER_NAME`, `GIT_USER_EMAIL` and `WORKBENCH_SCM_HOSTS`.

- **Fixed workdir path.** `init.workdir` is `mounted` at `/workdir/workbench`: workbench task files
  store absolute worktree paths, so the tree must be at the same path on every run. A `clone`
  alternative is in the example for a fresh checkout.
- **Plugin install.** The plugin directory is registered as a directory marketplace
  (`szymonrychu-workbench`) and `workbench@szymonrychu-workbench` is installed at user scope
  before claude starts; `superpowers@claude-plugins-official` is disabled so two flows never clash.
- **Rules copy.** `claude.config_files` copies `rules/` from the plugin into the session
  `CLAUDE_CONFIG_DIR`.
- **OAuth auth.** The plugin's `claude -p` helpers strip `ANTHROPIC_API_KEY`, so the example uses
  `CLAUDE_CODE_OAUTH_TOKEN`.
- **AskUserQuestion forwarding.** `sessions.ask_user_question: forward` turns the tool into a
  `question` event (also POSTed to the `callbackUrl`); the tool is denied with guidance, so the
  client answers by submitting the next turn (see [`question`](#question)).
- **Long-lived sessions and agent-origin turns.** `idle_timeout_seconds: 0` keeps sessions alive;
  turns the agent starts itself (for example from background tasks or scheduled work) appear as
  turns with `origin: agent` and fire the same hooks and events.
- **Git identity and forge tokens.** `init.git` sets the workdir identity from `GIT_USER_NAME` and
  `GIT_USER_EMAIL`; `GH_TOKEN`, `GITLAB_TOKEN`, `SSH_AUTH_SOCK` and `WORKBENCH_SCM_HOSTS` reach
  Claude through `claude.passthrough_env`, and the mounted `~/.ssh` serves git over SSH.

## CI

[![main](https://github.com/szymonrychu/claude-code-wrapper/actions/workflows/build.yaml/badge.svg)](https://github.com/szymonrychu/claude-code-wrapper/actions/workflows/build.yaml)

`.github/workflows/build.yaml` runs a strict chain of jobs, each `needs` the previous one:
`pre-commit` -> `unit` (lint + tests) -> `integration` (compose-based, real image, fake claude) ->
`build-push`. Pull requests run the whole chain and build the image, but never log in or push.
Only a push to `main` logs in to Harbor and pushes the image. The only registry used is Harbor
(no Docker Hub, no PyPI, no GHCR). The image is built as a gzip OCI archive and pushed with
`regctl` in 50MB chunks, because Cloudflare in front of Harbor rejects request bodies over 100MB
and some layers are larger than that.

Image: `harbor.szymonrichert.pl/containers/claude-code-wrapper`

- `:<shortsha>` (7 chars) for every commit on `main`.
- `:X.Y.Z` and `:latest` on release.

Harbor tags are immutable, so a tag is pushed once and never overwritten.

### Release

```bash
git tag vX.Y.Z && git push origin vX.Y.Z
```

The tagged commit must already have been built on `main`: `release.yaml` pulls `:<shortsha>`,
retags it `:X.Y.Z` and `:latest`, and pushes both. It fails with a clear message when the
`:<shortsha>` image does not exist.

### Secrets

- `HARBOR_USERNAME`: `robot$gitlab`
- `HARBOR_PASSWORD`: the robot account secret

### Run the stages locally

```bash
mise run precommit
mise run lint
mise run test
mise run test:integration
```

### Bootstrap (operator steps)

1. Create the public repo `szymonrychu/claude-code-wrapper`.
2. Set the secrets (single quotes: the `$` is literal):
   - `gh secret set HARBOR_USERNAME --body 'robot$gitlab' -R szymonrychu/claude-code-wrapper`
   - `gh secret set HARBOR_PASSWORD -R szymonrychu/claude-code-wrapper`
3. Run a secret scan over the full git history before the first public push.
4. Push `main`.
5. Watch the first pipeline go green, including the Harbor push.

## Development

```bash
mise run install    # uv sync
mise run test       # uv run pytest
mise run lint       # ruff check + ruff format --check
mise run fmt        # ruff format + autofix
mise run run        # serve with config.example.yaml
```

- **Fake claude.** `tests/fake_claude` is a scripted PTY program that stands in for `claude` (boot
  dialogs, bracketed-paste input, Stop hook, transcript writing), so the suite never needs a real
  Claude or network.
- **TDD.** Every behaviour change is written test-first: see the failing test, then the code.
  README coverage is itself tested: `tests/test_readme_api.py` and `tests/test_readme_config.py`
  fail on an undocumented route, event, hook, error code, config key or Claude env var. After
  changing a model, update the tables here (defaults are compared with the model).
- **Manual real-Claude smoke.**
  1. `export CLAUDE_CODE_OAUTH_TOKEN=... CCW_AUTH_TOKEN=smoke`
  2. `docker compose up -d --build`
  3. Run the [Quick start](#quick-start) curl sequence and confirm a `turn_completed` event with
     the assistant text arrives on the SSE stream.
  4. `docker compose logs ccw` should show JSON logs with no `fatal` events; then
     `docker compose down`.
