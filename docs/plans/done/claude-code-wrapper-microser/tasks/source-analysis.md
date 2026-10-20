---
title: Source analysis
status: done
repo: claude-code-wrapper
value: A record of what each source project uses and how it maps to the service
files: [docs/source-analysis.md]
verify: "test -s docs/source-analysis.md && mise run lint"
depends_on: [repo-scaffold]
owns: []
uses: []
model: ""
estimate: 75
actual: 4
review: skip
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/claude-code-wrapper-microser--source-analysis
branch: claude-code-wrapper-microser--source-analysis
verified_head: 0d25f5af360cbc5c3e02b16f7045eb30e4ea2134
run: claude-code-wrapper-microser
provides: []
acs_for_audit: [AC8]
process_gaps: []
---

# Source analysis

## Requirement

Write `docs/source-analysis.md` (D16), the analysis of what the three source projects use and how, mapped to this service.

- `## tatara-new`, `## magic-trainer`, `## ros2-lerobot-sverve-platform`: for each, which component drives Claude Code and how: process model and backend (PTY, SDK, -p), spawn flags and env, boot handling, turn submit and completion, event and tool-call sources, tool gating, lifecycle hooks, prompts and system instructions, MCP, plugins and skills, persistence and resume, auth, container/deploy shape, concurrency and timeouts, tests and fakes, pitfalls they hit. Every claim carries a file reference (path:line or path:symbol) into the source repo.
- `## Workbench compatibility`: what the workbench plugin needs from a host (tree root cwd, fixed path, plugin install, tools, auth, long-lived sessions, AskUserQuestion).
- `## Feature mapping`: a table with one row per service feature (PTY driver, boot dialogs, turn submit, Stop completion, transcript tailer, relay hooks, tool gate, AskUserQuestion forwarding, event log and SSE, hooks dispatcher, typed claude.env, workdir init, plugin install, mise, sessions and resume, auth, metrics, container) and columns: feature, sources (project + file), adopted as (as is, adapted, new), service module.
- `## Not adopted`: each source feature intentionally not taken (Agent SDK backend, `-p` review route, S3 resume, operator stall policy, metrics push, account usage statusline, domain flows) and why.
- Read the sources read-only: tatara `git -C /Users/szymonri/Documents/tatara-new/code/tatara-claude-code-wrapper show origin/main:<path>` (the checkout is on a side branch), magic `/Users/szymonri/Documents/magic-trainer/code/magic-cards-model-api/src/mtg_agent/wrapper`, ros2 `/Users/szymonri/Documents/ros2-lerobot-sverve-platform/nodes/claude_agent`. design.md "Source findings" is the starting point; confirm each reference against the source.

## Acceptance conditions

- AC1: The tatara section exists [evidence: doc-section:docs/source-analysis.md#tatara-new]
- AC2: The magic-trainer section exists [evidence: doc-section:docs/source-analysis.md#magic-trainer]
- AC3: The ros2 section exists [evidence: doc-section:docs/source-analysis.md#ros2-lerobot-sverve-platform]
- AC4: The workbench compatibility section exists [evidence: doc-section:docs/source-analysis.md#Workbench compatibility]
- AC5: The feature mapping table exists [evidence: doc-section:docs/source-analysis.md#Feature mapping]
- AC6: The not adopted section exists [evidence: doc-section:docs/source-analysis.md#Not adopted]
- AC7: The document is plain ASCII [evidence: command:LC_ALL=C grep -q '[^ -~]' docs/source-analysis.md && exit 1 || exit 0]
- AC8: Every claim carries a source file reference that matches the source repo, and every service feature row names at least one source or is marked new [evidence: judgement:]

## Files

- `docs/source-analysis.md`

## Interfaces

- Documentation only; no code interfaces.

## Expected outcome

A reader can see, per source project, what Claude Code functionality is used and how, and where each piece landed (or why it did not) in this service.

## Steps

- Docs task (TDD exception: documentation); write the document from design.md "Source findings", confirming references against the sources; run the ASCII check and `mise run lint`.

## Notes

- Tabs are outside the printable ASCII range of the check; use spaces.
- The briefs that produced design.md were temporary; design.md "Source findings" carries their content.
