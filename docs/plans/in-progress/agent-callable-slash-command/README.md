---
title: Agent-callable slash commands and idle auto-compact
blockers: [Hook capabilities for tatara and magic-trainer parity]
stage: build
owner: Szymon Richert
repos: [.]
created: 2026-10-21
---

# Agent-callable slash commands and idle auto-compact

## Project brief

What: Internal MCP tool and public endpoint to queue allowlisted Claude Code slash commands (compact, reload-plugins, ...), run at the turn's Stop with same-turn resume, plus optional idle auto-compact
How: ccw MCP server over HTTP on the internal API, a per-session command queue in the router, defer-and-continue via the stop gate and K12 continuation, completion via PostCompact hook and transcript system entries
Why: Long-lived agents need to manage their own context and pick up plugin changes without a human at the keyboard

## Description

Claude Code slash commands such as /compact and /reload-plugins are not tools the agent can call. ccw drives claude under a PTY and can type them, so it gives the main agent an internal MCP tool to queue such commands, runs them at the turn's Stop and resumes the agent in the same turn. Clients can queue the same commands through the public API, and an optional idle timer queues /compact by itself.

## Goals

- Main agent can self-compact, self-reload plugins and run the other allowlisted slash commands through one ccw-owned MCP tool, `mcp__ccw__command`.
- Commands run at the turn's Stop and the agent resumes in the same turn with a continuation prompt; idle sessions run them immediately.
- Clients can queue commands with POST /v1/sessions/{id}/commands and read them back.
- Optional auto-compact after a configured inactivity time.
- Events, hooks (onCommand veto, onCommandComplete), metrics and README docs for all of it.
- Transcript parser no longer treats compact summaries and command echoes as prompts (stale marker fix).

## Non-goals

- Session-replacing commands (/clear, /new, /reset, /resume, /rewind, /exit, /login) and dialog or skill commands.
- Auto --force for /reload-plugins.
- Calls from subagents or teammates (refused, main agent only).
- Changing Claude Code's own idleCompaction behaviour.

## Time

Estimate: 1035 min | Actual: 27 min | Remaining estimate: 225 min

## Review

Filled when the epic reaches stage `review`: the verdict, the findings acted on, the findings dropped.
