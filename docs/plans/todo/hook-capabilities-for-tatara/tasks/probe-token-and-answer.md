---
title: Probe token and answer
status: todo
repo: .
value: "Probes answer with the operator's own token and expose the answer text (K9)"
files: [src/claude_code_wrapper/config.py, src/claude_code_wrapper/router.py, src/claude_code_wrapper/session.py, src/claude_code_wrapper/turns.py, src/claude_code_wrapper/api/turns.py, README.md, tests/test_probe_token.py]
verify: "uv run pytest tests/test_probe_token.py tests/test_session_turns.py tests/test_api_turns.py tests/test_readme_config.py -q && mise run lint"
depends_on: [ontick-timed-hook]
owns: [probe-alive-token]
uses: [ccw-world-fixture]
model: sonnet
estimate: 60
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Probe token and answer

## Requirement

K9 configurable probe token and captured answer (design D13). Owns part `probe-alive-token`.

- `sessions.probe_alive_token: str = "CCW-ALIVE"` (non-empty) replaces the hard-coded `ALIVE_TOKEN` in router.py. The default probe text names the configured token ("Reply with <token> and one sentence ...").
- `Probe.answer: str | None`: the main-chain assistant text that answered the probe (contains the token), capped at 2000 characters, set together with `answered_at`.
- `GET /v1/sessions/{id}/probe/{probe_id}` and the `probe` event include `answer`.
- README: the config row, the probe sections and the `probe` event.

## Acceptance conditions

- AC1: a configured token answers the probe and CCW-ALIVE no longer does [evidence: test:tests/test_probe_token.py::test_custom_token_answers_probe]
- AC2: the default token is still CCW-ALIVE [evidence: test:tests/test_probe_token.py::test_default_token_unchanged]
- AC3: the answer text is captured and capped at 2000 characters [evidence: test:tests/test_probe_token.py::test_answer_captured_and_capped]
- AC4: GET probe returns the answer [evidence: test:tests/test_probe_token.py::test_get_probe_returns_answer]
- AC5: the default probe text names the configured token [evidence: test:tests/test_probe_token.py::test_default_probe_text_names_configured_token]
- AC6: an empty token is a config error [evidence: test:tests/test_probe_token.py::test_empty_token_rejected]
- AC7: README documents the key and the answer field [evidence: command:uv run pytest tests/test_readme_config.py -q && grep -q probe_alive_token README.md]

## Files

- `src/claude_code_wrapper/config.py`
- `src/claude_code_wrapper/router.py`
- `src/claude_code_wrapper/session.py`
- `src/claude_code_wrapper/turns.py`
- `src/claude_code_wrapper/api/turns.py`
- `README.md`
- `tests/test_probe_token.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
