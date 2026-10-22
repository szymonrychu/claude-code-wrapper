---
title: Agent-callable slash commands and idle auto-compact
blockers: [Hook capabilities for tatara and magic-trainer parity]
stage: review
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

Estimate: 1110 min | Actual: 27 min | Remaining estimate: 75 min

## Review

Filled when the epic reaches stage `review`: the verdict, the findings acted on, the findings dropped.

Verdict: pass (2026-10-22)

Epic review, agent-callable-slash-command, 2026-10-22.
- 16 tasks done, verified and merged into main (last merge d173664). Hand-verified after verifier failures (verify-agent-died, a hung verifier, --prev-head pending): command-registry-and-config, command-runner-failures, deflake-never-typed-while-bu, idle-auto-compact, commands-public-endpoint, slash-epic-review-fixes; command-runner accepted over a false parking finding; rulings in each task file. Dropped discovered task confirm-the-public-command-e (duplicate).
- Whole-diff review (sonnet): no critical/high; findings fixed in slash-epic-review-fixes: README command_* record fields, extra top-level tool arguments silently dropped, top-level oneOf in the tool input_schema (Anthropic API rejects it) replaced by a flat schema.
- Security review (sonnet): HIGH /mcp reconnect server param smuggling fixed (all string params validated, strict server pattern); MEDIUM subagent direct /internal/mcp call documented as accepted under the trust model.
- Scope audit (auditOnly run wf_7c49cee7-0a4): one gap (config.example.yaml comment broke test_packaging), fixed in slash-epic-review-fixes.
- Suites: full suite 1102 passed, 7 deselected on b4d087b (= main code); integration 7 passed; lint clean; pre-commit 9 hooks passed.
- Merge-time fixes on main by the orchestrator: 1591dcd README rows for hooks.onCommand/onCommandComplete, f874f1e StubSession command_active, b4d087b restored README {command_id} route path.
- Not verifiable here: live Anthropic API acceptance of the flat tool schema and live ${CCW_*} header expansion beyond the planner's claude mcp list probe; covered on the next real-claude run.
