---
title: Peer session bootstrap
status: todo
repo: .
value: A session can be launched with its socket on the shared mount and accept peer messages in bypass mode
files: [src/claude_code_wrapper/session.py, src/claude_code_wrapper/bootstrap/settings.py, src/claude_code_wrapper/bootstrap/instructions.py, tests/test_peers_bootstrap.py]
verify: "uv run pytest tests/test_peers_bootstrap.py tests/test_session_boot.py tests/test_bootstrap_settings.py tests/test_bootstrap_home.py -q && mise run lint"
depends_on: []
owns: [peer-session-bootstrap]
uses: []
model: sonnet
estimate: 50
actual: ""
review: ""
worktree: ""
branch: ""
verified_head: ""
run: ""
provides: []
---

# Peer session bootstrap

## Requirement

The per-session pieces a peers-enabled session needs, as pure building blocks the manager wires in (design D3, D6; facts F2, F6, F8). Owns part `peer-session-bootstrap` (src/claude_code_wrapper/session.py, bootstrap/settings.py, bootstrap/instructions.py). With the new parameters at their defaults every output is byte-identical to today.

- session.py `SessionSpec` gains `messaging_socket_path: str = ""`. `_argv` (session.py:164) appends `["--messaging-socket-path", spec.messaging_socket_path]` right after the `--session-id`/`--resume` pair and before `extra_args`, for both a fresh launch and a resume, when the field is non-empty; empty leaves argv unchanged.
- session.py `build_env(...)` gains keyword `peer_env: Mapping[str, str] | None = None`, layered after `mise_env` and before the service-managed vars (so ccw's values win over a user's `claude.env.extra` of the same name). None or empty leaves the env unchanged. The names are not added to `MANAGED_ENV_NAMES` (that would change passthrough behaviour with peers off).
- bootstrap/settings.py: constant `CCW_PEERS_ALLOW = "mcp__ccw__peers"`; `render_settings(config_dir, cfg, relay_command=None, ccw_mcp=False, peers=False)`:
  - `peers=True`: `doc["crossSessionInbound"] = "accept"` before the user overlay is merged (an explicit `claude.settings.crossSessionInbound` overlay still wins, my call), and `allow` gains `CCW_PEERS_ALLOW`.
  - `peers=False`: `CCW_PEERS_ALLOW` is filtered out of `allow` like `CCW_MCP_ALLOW` is today, and a `crossSessionInbound` of `"accept"` left by an earlier peers-enabled render is removed before the overlay (so turning peers off and resuming restores today's settings).
- bootstrap/instructions.py: constant `PEERS_INSTRUCTIONS` (plain ASCII, one paragraph): peer messaging is on; other Claude sessions in this and other ccw containers sharing the peers mount can message you and you can message them; call the ccw `peers` tool to list live peers with their `uds:` addresses, then use SendMessage with `to` set to a peer's address; peer messages arrive as `<agent-message>` turns; reply with SendMessage to the sender's `from` address. `write_instructions(config_dir, prompts_cfg, peers=False)`: `peers=True` appends the paragraph as the last part (so CLAUDE.md is written even with no prompts configured); `peers=False` behaves exactly as today (including removing a ccw-written CLAUDE.md when there are no parts).
- tests/test_peers_bootstrap.py covers all of the above with plain function calls (no manager).

Interfaces: `SessionSpec.messaging_socket_path`, `build_env(..., peer_env=...)`, `render_settings(..., peers=...)`, `CCW_PEERS_ALLOW`, `write_instructions(..., peers=...)`, `PEERS_INSTRUCTIONS`.

Notes: `crossSessionInbound: accept` in user settings is what stops a bypass-mode receiver from holding every peer message for a TUI approval nobody can give (fact F6). Claude reads the explicit `--messaging-socket-path` even under `--bare` (fact F8).

## Acceptance conditions

- AC1: a non-empty messaging_socket_path adds the flag after the session id on launch and on resume, and an empty one leaves argv unchanged [evidence: test:tests/test_peers_bootstrap.py::test_argv_messaging_socket_path]
- AC2: build_env layers peer_env over claude env and mise env, and None leaves the env unchanged [evidence: test:tests/test_peers_bootstrap.py::test_build_env_peer_env_layering]
- AC3: render_settings with peers writes crossSessionInbound accept and allows mcp__ccw__peers, and a user overlay value wins [evidence: test:tests/test_peers_bootstrap.py::test_settings_peers_on]
- AC4: render_settings without peers drops a ccw-written accept and the peers allow rule and otherwise matches today's output [evidence: test:tests/test_peers_bootstrap.py::test_settings_peers_off_restores]
- AC5: write_instructions with peers appends the paragraph naming the peers tool, SendMessage and uds addresses, even with no prompts [evidence: test:tests/test_peers_bootstrap.py::test_instructions_peers_paragraph]
- AC6: existing session, settings and instructions suites stay green [evidence: command:uv run pytest tests/test_session_boot.py tests/test_bootstrap_settings.py tests/test_bootstrap_home.py tests/test_mcp_injection.py -q]

## Files

- `src/claude_code_wrapper/session.py`
- `src/claude_code_wrapper/bootstrap/settings.py`
- `src/claude_code_wrapper/bootstrap/instructions.py`
- `tests/test_peers_bootstrap.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
