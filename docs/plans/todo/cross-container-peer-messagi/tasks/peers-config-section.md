---
title: Peers config section
status: todo
repo: .
value: Operators turn peer messaging on with one validated config section and nothing changes when it is off
files: [src/claude_code_wrapper/config.py, tests/test_peers_config.py, README.md, src/claude_code_wrapper/data/config.example.yaml]
verify: "uv run pytest tests/test_peers_config.py tests/test_config.py tests/test_readme_config.py tests/test_examples.py -q && mise run lint"
depends_on: []
owns: [peers-config]
uses: []
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

# Peers config section

## Requirement

Add the opt-in `peers:` config section (design D2).

- src/claude_code_wrapper/config.py: `class PeersConfig(_Model)` with `enabled: bool = False`, `name: str = ""`, `advertise_url: str = ""`, `token: str = ""`, `directory: str = "/peers"`, `heartbeat_seconds: float = 10`, `stale_seconds: float = 30`, `max_message_bytes: int = 65536`, `inbox_max: int = 32`, and `Config.peers: PeersConfig = Field(default_factory=PeersConfig)`.
- Validation, each error reported at its `peers.<key>` path through `load_config`:
  - `token` empty while `enabled` -> `peers.token: required when peers.enabled; set it, normally from ${CCW_PEER_TOKEN}`; shorter than 16 characters -> error; equal to a non-empty `server.auth_token` -> `peers.token: must differ from server.auth_token`.
  - `name` empty, or matching `[A-Za-z0-9._-]{1,64}`; anything else is an error.
  - `advertise_url` empty, or an absolute `http://` or `https://` URL with a host and no trailing slash; the literal `{port}` is allowed anywhere in it.
  - `directory` absolute.
  - `heartbeat_seconds > 0`; `stale_seconds > heartbeat_seconds`; `1 <= max_message_bytes <= 1048576`; `inbox_max >= 1`.
  - With `peers.enabled`, a user `claude.mcp_servers.ccw` is an error at `claude.mcp_servers.ccw` (extend `Config._ccw_name_reserved`).
  - With `enabled: false` the token and every other key are not required, so an absent section loads exactly as today.
- `Config.redacted()` replaces a non-empty `peers.token` with `***`.
- README.md: under `## Configuration reference` a `### \`peers\`` subsection with a `Key | Type | Default | Env var | Description` row for every `peers.*` key (tests/test_readme_config.py enforces it); the token row says it is required when enabled, never logged and masked.
- src/claude_code_wrapper/data/config.example.yaml: a `peers:` section with `enabled: false` and every key at its default with a one-line comment, the token as `${CCW_PEER_TOKEN:-}`, loadable with no env set.
- tests/test_peers_config.py covers the defaults, every validation rule above, the redaction and that the example config loads with peers disabled.

Interfaces: `config.PeersConfig`, `Config.peers` (the `peers-config` contract).

## Acceptance conditions

- AC1: an absent peers section gives enabled false and the stated defaults [evidence: test:tests/test_peers_config.py::test_defaults_disabled]
- AC2: enabled without a token, with a short token or with the token equal to server.auth_token fails at peers.token [evidence: test:tests/test_peers_config.py::test_token_rules]
- AC3: invalid name, advertise_url, directory, heartbeat/stale, max_message_bytes and inbox_max values fail at their peers path, and {port} in advertise_url is accepted [evidence: test:tests/test_peers_config.py::test_key_validation]
- AC4: a user claude.mcp_servers.ccw is rejected when peers are enabled [evidence: test:tests/test_peers_config.py::test_ccw_name_reserved_with_peers]
- AC5: redacted() masks peers.token [evidence: test:tests/test_peers_config.py::test_token_redacted]
- AC6: every peers key has a README config row and the example config loads [evidence: command:uv run pytest tests/test_readme_config.py tests/test_config.py tests/test_examples.py -q]

## Files

- `src/claude_code_wrapper/config.py`
- `tests/test_peers_config.py`
- `README.md`
- `src/claude_code_wrapper/data/config.example.yaml`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
