---
title: claude-code wrapper microservice
blockers: []
stage: build
owner: Szymon Richert
repos: [claude-code-wrapper]
created: 2026-10-20
---

# claude-code wrapper microservice

## Project brief

What: A standalone Python HTTP microservice that runs N long-lived interactive Claude Code sessions, each under a PTY in its own workdir, with a turns API, SSE event stream, config.yaml-driven setup (init, prompts, typed Claude Code env vars, mise tooling, plugin pre-install) and user-defined lifecycle hooks (onCreate, onTurn, onAgentResponse, onAgentToolCall, onDestroy and more), shipped as a container with docker-compose volumes for workdirs and past sessions.
How: Extract the PTY-driving methodology already proven in tatara-claude-code-wrapper (Go) and its Python port in magic-trainer (bracketed paste, Stop hook to a loopback endpoint, JSONL transcript tailing, boot dialog handling), add the ros2 service ideas (event log, SSE-style stream, tool gate, bounded shutdown), wire every Claude hook through a relay to the service, and build it test-first against a scripted fake `claude` PTY program.
Why: Three projects each carry their own copy of a Claude Code wrapper; one configurable service that is compatible with the workbench plugin flow lets any project (and the workbench itself) drive Claude Code over HTTP without re-implementing the fragile parts.

## Description

The service (`claude-code-wrapper`, CLI `ccw`) manages sessions. A session is one interactive `claude` process under a PTY, started in a workdir on the `workdir` volume, with its own `CLAUDE_CONFIG_DIR` on the `sessions` volume so transcripts, plugins and trust survive restarts and a past session can be resumed by id. Clients create sessions, submit turns, poll turns and transcripts, and stream events over SSE. config.yaml defines server and auth settings, the Claude binary and its typed environment (every known Claude Code env var as a validated field, with per-session overrides and secret redaction), workdir initialisation (empty, mounted or git clone, setup commands), prompts (global instructions, named templates, initial prompt), mise tooling, plugin marketplaces and plugins to pre-install (the workbench plugin among them), and hooks. Hooks are lists of command or webhook actions per event; blocking actions can veto a turn or deny a tool call. A README documents every endpoint, event, hook and config key; docs/source-analysis.md records what each source project does and how it maps to the service.

## Goals

- `docker compose up` starts the service from the repo with named volumes `workdir` and `sessions` and a mounted config.yaml; it runs as a non-root user.
- A client can create a session, submit a turn, receive assistant text, tool calls and turn completion over SSE and by polling, interrupt, destroy, and later resume the same Claude session by id.
- config.yaml covers initialisation, prompts, mise tooling, plugin pre-install, hooks, sessions and limits, with `${VAR}` interpolation; `claude.env` models every known Claude Code env var as a typed field with a free-form `extra` passthrough, per-session override and redaction.
- Hooks onCreate, onResume, onDestroy, onTurn, onTurnComplete, onAgentResponse, onAgentToolCall, onToolResult, onSubagentStart, onSubagentStop, onInterrupt and onError fire as command or webhook actions; a blocking onAgentToolCall can deny a tool call and a blocking onTurn can veto a turn.
- The workbench plugin flow works inside a session: workdir is a workbench tree root at a fixed absolute path, the plugin installs from a mounted directory marketplace, the image ships the plugin's tool needs.
- Test-first throughout; an end-to-end test drives the full lifecycle through a fake `claude` without real Claude.
- README.md covers every endpoint, SSE event, hook, config key, docker/compose and workbench usage; docs/source-analysis.md covers the three source projects.

## Non-goals

- No Claude Agent SDK backend and no `claude -p` one-shot route (user decision D1).
- No WebSocket stream; SSE plus polling only.
- No Kubernetes manifests, Helm chart, operator or metrics push.
- No remote, CI pipeline, image publishing or deploy; delivery is a local merge to main.
- No domain logic from the source projects (robot tools, game seats, tatara task flow, auto commit/push); those belong in user hooks.

## Time

Estimate: 2085 min | Actual: 20 min | Remaining estimate: 1770 min

## Review

Filled when the epic reaches stage `review`: the verdict, the findings acted on, the findings dropped.
