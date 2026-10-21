---
title: Probe token and answer
status: in-progress
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

- Part `probe-alive-token`: `SessionsConfig.probe_alive_token`, `turns.py::Probe.answer`, `Session.probe_answered(text)`.

## Expected outcome

A probe is answered by the configured token (e.g. TATARA-ALIVE) and the answer text is visible in GET probe and the probe event.

## Steps

- Write `tests/test_probe_token.py` (`world` with a fake turn answering the token); see it fail.
- Replace `ALIVE_TOKEN`, thread the token into the default probe text, capture the answer.
- README.
- `uv run pytest tests/test_probe_token.py tests/test_session_turns.py tests/test_api_turns.py tests/test_readme_config.py -q && mise run lint`.

## Notes

- Probe state is core (session.py probe section); keep pending -> delivered -> answered.
