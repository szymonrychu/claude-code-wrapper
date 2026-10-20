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
- The service must not be exposed to untrusted callers. If `server.auth_token` is empty,
  authentication on `/v1` is disabled and the service logs a warning at start.
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
`Authorization: Bearer <server.auth_token>` (constant-time compare; an empty token disables auth).
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
{"step", "message"}`); `current_turn_id` is the running turn; `last_fatal` is the last fatal
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

Response `200`: the stopped snapshot; with `purge=true` it also carries `"purged": true`.

| Status | Code | When |
|---|---|---|
| 200 | | stopped (idempotent on an already stopped session) |
| 401 | `unauthorized` | missing or wrong token |
| 404 | `not_found` | unknown session |
| 409 | `invalid_state` | the session is still `creating` |
| 422 | `invalid_request` | `purge` is not a boolean |

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
| 413 | `too_large` | prompt over `limits.max_prompt_bytes`, or `files` over the per-file or count limits |
| 422 | `invalid_request` | invalid body, or `unknown_template`, `unknown_variable`, `missing_variable` |
| 503 | `booting` | the session is still booting |
| 503 | `dead` | the session is stopped, failed or dead |

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
  "tool_calls": 2
}
```

Fields: `state` is `running`, `completed`, `failed`, `interrupted` or `timeout`; `final_text` is
Claude's last assistant message (set on completed turns); `error` names why a turn failed
(`claude_exited`, `session_stopped`, or the fatal pattern matched on screen); `usage` sums token
counts across models, `model_tokens` breaks them down per model; `stall_suspected_since` is set
while no activity has been seen for `sessions.turn_stall_seconds` (reported, never fatal);
`tool_calls` counts tool results seen.

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
asks Claude to reply with `CCW-ALIVE`; it is `pending` until the transcript shows it was
delivered, `delivered` after, and `answered` once an assistant message containing `CCW-ALIVE`
arrives. State changes of an answered probe emit a `probe` event.

Auth: bearer. Path parameter: `id`. Request body (optional):

| Field | Type | Required | Meaning |
|---|---|---|---|
| `text` | string | no | probe text; the default asks Claude to reply with `CCW-ALIVE` and continue |

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
 "answered_at": "2026-10-20T09:02:03.250+00:00"}
```

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
`ccw_hook_actions_total`, `ccw_claude_restarts_total`, `ccw_turn_tokens_total`). No auth.
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
`PostToolUseFailure`, `Stop`, `SubagentStart`, `SubagentStop`. Request body: the raw JSON object
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
data: {"seq":12,"ts":"2026-10-20T09:01:09.455Z","session_id":"ses_3f9a1c0b7d42","type":"turn_completed","data":{"record":{"turn_id":"turn-1k9x2m0q4zw8","session_id":"ses_3f9a1c0b7d42","origin":"client","state":"completed","text":"hi","final_text":"Hello!","stop_reason":"end_turn","usage":{"input_tokens":120,"output_tokens":48},"model_tokens":null,"error":null,"callbackUrl":null,"started_at":"2026-10-20T09:01:00.010+00:00","completed_at":"2026-10-20T09:01:09.455+00:00","last_activity_at":null,"stall_suspected_since":null,"tool_calls":0}}}
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

### `tool_call`

Emitted from the PreToolUse relay, before the tool runs (and before `onAgentToolCall` fires).

`data`: `tool_use_id`, `tool_name`, `tool_input` (as Claude sent it; a plugin hook that rewrites
Bash through `updatedInput` can make the executed command differ from this).

```
event: tool_call
data: {"seq":11,"ts":"2026-10-20T09:01:06.000Z","session_id":"ses_3f9a1c0b7d42","type":"tool_call","data":{"tool_use_id":"toolu_01","tool_name":"Bash","tool_input":{"command":"ls"}}}
```

### `tool_result`

Emitted from the PostToolUse and PostToolUseFailure relays.

`data`: `tool_use_id`, `tool_name`, `is_error` (boolean), `response` (text, truncated when very
long).

```
event: tool_result
data: {"seq":12,"ts":"2026-10-20T09:01:07.000Z","session_id":"ses_3f9a1c0b7d42","type":"tool_result","data":{"tool_use_id":"toolu_01","tool_name":"Bash","is_error":false,"response":"README.md\nsrc"}}
```

### `tool_denied`

Emitted when a blocking `onAgentToolCall` action denied a tool call; Claude receives the reason.

`data`: `tool_use_id`, `tool_name`, `reason`.

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
`answered_at`.

```
event: probe
data: {"seq":20,"ts":"2026-10-20T09:02:03.250Z","session_id":"ses_3f9a1c0b7d42","type":"probe","data":{"probe_id":"probe-1k9x2n7c1a5d","state":"answered","text":"Reply with CCW-ALIVE ...","created_at":"2026-10-20T09:02:00.100+00:00","delivered_at":"2026-10-20T09:02:00.700+00:00","answered_at":"2026-10-20T09:02:03.250+00:00"}}
```

### `hook_result`

Emitted for every user hook action that ran (command or webhook), blocking or not.

`data`: `event` (hook event name), `index` (action index in the event's list), `type` (`command` or
`webhook`), `ok` (false when the action errored), `decision` (`allow` or `deny`), `duration_ms`,
`error` (null or the redacted failure).

```
event: hook_result
data: {"seq":21,"ts":"2026-10-20T09:01:06.100Z","session_id":"ses_3f9a1c0b7d42","type":"hook_result","data":{"event":"onAgentToolCall","index":0,"type":"command","ok":true,"decision":"deny","duration_ms":41,"error":null}}
```

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
  "data": {"text": "hi", "files": [], "origin": "client"}
}
```

`turn_id` is the turn the event belongs to, else the running turn, else null. `data` per event is
below.

### `onCreate`

Trigger: the manager, after claude is ready on a new session. Blocking: a `deny` aborts creation
(the session is stopped; `POST /v1/sessions` answers `403 vetoed`). `data`: the session snapshot.

### `onResume`

Trigger: the manager, after a resumed claude is ready. Blocking: a `deny` aborts the resume
(`403 vetoed`). `data`: the session snapshot.

### `onDestroy`

Trigger: the manager, after claude stopped. Awaited with a bounded budget; cannot veto. `data`: the
stopped session snapshot.

### `onTurn`

Trigger: the router, before the prompt is pasted (also for `initial` and `agent` origins). Blocking:
a `deny` vetoes the turn (`403 vetoed`, `turn_rejected` event). `data`: `text`, `files` (list of
workdir-relative paths about to be written), `origin`.

### `onTurnComplete`

Trigger: every terminal turn state: the Stop relay for `completed`, and the router for `failed`,
`interrupted` and `timeout`. Cannot veto. `data`: the full turn record (same fields as
`GET /v1/sessions/{id}/turns/{turn_id}`), with `state` telling which terminal state was reached.
Turn callbacks (`callbackUrl`) follow the same rule.

### `onAgentResponse`

Trigger: each main-thread assistant text block read from the transcript (subagent text excluded).
Cannot veto. `data`: `text`, `message_id`, `model`.

### `onAgentToolCall`

Trigger: Claude's PreToolUse, relayed before the tool runs. Supports `matcher`. Blocking: a `deny`
blocks the tool call (Claude is told the reason, a `tool_denied` event is logged); an action's
`additional_context` is returned to Claude. `data`: `tool_use_id`, `tool_name`, `tool_input`.

### `onToolResult`

Trigger: PostToolUse and PostToolUseFailure. Supports `matcher`. Cannot veto; a blocking action's
`additional_context` is returned to Claude as hook context. `data`: `tool_use_id`, `tool_name`,
`is_error`, `response`.

### `onSubagentStart`

Trigger: SubagentStart. Awaited when blocking; cannot veto. `data`: `agent_id`, `agent_type`.

### `onSubagentStop`

Trigger: SubagentStop. Awaited when blocking; cannot veto. `data`: `agent_id`, `agent_type`,
`last_assistant_message` (when provided).

### `onInterrupt`

Trigger: the router, when an interrupt request is accepted. Cannot veto. `data`: `turn_id`.

### `onError`

Trigger: fatal screen pattern, claude exit or restart, create or resume step failure, hook relay
failure. Cannot veto. `data`: `kind` (`fatal`, `exited`, `restart`, `hook_relay`, `create_failed`,
`resume_failed`), `message`, plus `tail` (fatal), `code` (exited), `count` (restart) or `step`
(create and resume failures).

### Command actions

The payload envelope is written to stdin. Environment: the service's child environment plus the
action's `env`, and `CCW_EVENT`, `CCW_SESSION_ID`, `CCW_TURN_ID`, `CCW_WORKDIR`,
`CCW_CLAUDE_CONFIG_DIR`. The working directory is the session workdir. The process runs in its own
process group and is killed with its children on timeout.

Exit codes: `0` allow; `2` deny, with trimmed stderr (up to 2000 chars) as the reason; any other
exit code, or a timeout, is an error handled by `on_error`.

Stdout on exit 0 may be a JSON object; other output is ignored:

```json
{"decision": "deny", "reason": "writes are blocked", "additional_context": "Use the staging branch."}
```

`decision` is `allow` or `deny`, `reason` is shown to Claude on a deny, `additional_context` is
appended to what Claude sees (for `onAgentToolCall` and `onToolResult`).

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
uses the same decision shape as command stdout.

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
