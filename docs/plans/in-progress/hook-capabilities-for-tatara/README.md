---
title: Hook capabilities for tatara and magic-trainer parity
blockers: []
stage: build
owner: Szymon Richert
repos: [.]
created: 2026-10-21
---

# Hook capabilities for tatara and magic-trainer parity

## Project brief

What: Generic ccw hook capabilities, core options and defect fixes that let tatara and magic-trainer gaps be closed with hooks, plus worked examples for both
How: New hook events (onStop, onTick), hook result fields (block/defer, annotations, output), destroy hook input/output, hook context env, agent attribution, turn metadata, exit policy, probe token, subagent count, passthrough globs, turn continuity, metric families; examples under data/examples tested with the fake claude
Why: So both projects can later move onto ccw without ccw growing project-specific code

## Description

ccw already matches the tatara and magic-trainer harnesses on the interactive core (PTY, boot, submit, Stop completion, transcripts). The parity research (2026-10-21) found the remaining gaps. The user decided they are closed with generic ccw hook capabilities, so domain logic lives in hook scripts, not in ccw core. Where hooks cannot do it, a minimal generic core change is made instead. Three ccw defects found by the same research are fixed too.

## Goals

- Every non-deferred tatara and magic-trainer gap is implementable with ccw hooks or config, using the generic capabilities K1-K13 from the design.
- magic-trainer review runs as an optional onDestroy hook (one-shot claude -p before teardown), its output returned in the DELETE response and as an event; an option exits the process when the last session is destroyed.
- Defects fixed: PreToolUse gate fails closed, busy check before turn files, hook env documented correctly.
- Examples in this repo show how tatara and magic-trainer would configure and script these hooks, each tested against the fake claude.
- README documents every new event, result field, option and env var.

## Non-goals

- Replacing the harness in tatara or magic-trainer (no consumer repo changes).
- OIDC / Keycloak JWT auth for tatara.
- API reshaping for tatara (paths, default session per pod, field names, contractVersion, compat facade).
- ros2-lerobot-sverve-platform (deferred entirely).
- Relaying TeammateIdle / TaskCompleted as hook events.

## Time

Estimate: 1380 min | Actual: 96 min | Remaining estimate: 270 min

## Review

Filled when the epic reaches stage `review`: the verdict, the findings acted on, the findings dropped.
