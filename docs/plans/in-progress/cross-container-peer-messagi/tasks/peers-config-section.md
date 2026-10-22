---
title: Peers config section
status: in-progress
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
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/cross-container-peer-messagi--peers-config-section
branch: cross-container-peer-messagi--peers-config-section
verified_head: ""
run: ""
provides: []
---

# Peers config section

## Requirement

Add the opt-in `peers:` config section (design D2).

- src/claude_code_wrapper/config.py: `class PeersConfig(_Model)` with `enabled: bool = False`, `name: str = ""`, `advertise_url: str = ""`, `token: str = ""`, `static: list[str] = []` (each item a URL string or `{url: <string>}`, normalized to strings by a before-validator), `heartbeat_seconds: float = 10`, `max_missed_heartbeats: int = 3`, `join_timeout_seconds: float = 10`, `max_message_bytes: int = 65536`, `inbox_max: int = 32`, `max_clock_skew_seconds: float = 60`, `max_messages_per_minute: int = 30`, and `Config.peers: PeersConfig = Field(default_factory=PeersConfig)`.
- Validation, each error reported at its `peers.<key>` path through `load_config`:
  - `token` empty while `enabled` -> `peers.token: required when peers.enabled; set it, normally from ${CCW_PEER_TOKEN}`; shorter than 16 characters -> error; equal to a non-empty `server.auth_token` -> `peers.token: must differ from server.auth_token`.
  - `name` empty, or matching `[A-Za-z0-9._-]{1,64}` and not `.`, `..` or starting with `.`.
  - `advertise_url` empty, or an absolute `http://` or `https://` URL with a host and no trailing slash; the literal `{port}` is allowed anywhere in it.
  - `static`: every item an absolute http(s) URL with a host and no trailing slash (error at `peers.static.<index>`); duplicates removed keeping order.
  - `heartbeat_seconds > 0`; `max_missed_heartbeats >= 1`; `join_timeout_seconds > 0`; `1 <= max_message_bytes <= 1048576`; `inbox_max >= 1`; `max_clock_skew_seconds > 0`; `max_messages_per_minute >= 1`.
  - With `peers.enabled`, a user `claude.mcp_servers.ccw` is an error at `claude.mcp_servers.ccw` (extend `Config._ccw_name_reserved`).
  - With `enabled: false` nothing is required, so an absent section loads exactly as today.
- `PeersConfig.with_static(urls) -> PeersConfig`: a copy whose `static` is the configured list extended by `urls` (validated the same way, de-duplicated), for `ccw serve --peer`.
- `Config.redacted()` replaces a non-empty `peers.token` with `***`.
- README.md: under `## Configuration reference` a `### \`peers\`` subsection with a `Key | Type | Default | Env var | Description` row for every `peers.*` key (tests/test_readme_config.py enforces it); the token row says it is required when enabled, a signing key only, never logged and masked; the static row mentions `--peer`.
- src/claude_code_wrapper/data/config.example.yaml: a `peers:` section with `enabled: false` and every key at its default with a one-line comment, the token as `${CCW_PEER_TOKEN:-}`, loadable with no env set.
- tests/test_peers_config.py covers the defaults, every validation rule above, string and `{url}` static items, `with_static`, the redaction and that the example config loads with peers disabled.

Interfaces: `config.PeersConfig`, `Config.peers`, `PeersConfig.with_static` (the `peers-config` contract).

## Acceptance conditions

- AC1: an absent peers section gives enabled false and the stated defaults [evidence: test:tests/test_peers_config.py::test_defaults_disabled]
- AC2: enabled without a token, with a short token or with the token equal to server.auth_token fails at peers.token [evidence: test:tests/test_peers_config.py::test_token_rules]
- AC3: invalid name (including ., .. and a leading dot), advertise_url, heartbeat, missed-heartbeat, timeout, size, inbox, skew and rate values fail at their peers path, and {port} in advertise_url is accepted [evidence: test:tests/test_peers_config.py::test_key_validation]
- AC4: static accepts strings and {url} items, rejects bad URLs at their index, de-duplicates, and with_static extends it [evidence: test:tests/test_peers_config.py::test_static_seeds]
- AC5: a user claude.mcp_servers.ccw is rejected when peers are enabled [evidence: test:tests/test_peers_config.py::test_ccw_name_reserved_with_peers]
- AC6: redacted() masks peers.token [evidence: test:tests/test_peers_config.py::test_token_redacted]
- AC7: every peers key has a README config row and the example config loads [evidence: command:uv run pytest tests/test_readme_config.py tests/test_config.py tests/test_examples.py -q]

## Files

- `src/claude_code_wrapper/config.py`
- `tests/test_peers_config.py`
- `README.md`
- `src/claude_code_wrapper/data/config.example.yaml`


## Interfaces

See the Requirement's Interfaces paragraph; keep every name and signature stated there.

## Expected outcome

A config without a peers section loads exactly as today; an enabled one is validated with path-precise errors and its token is masked.

## Steps

- Write the tests named in the acceptance conditions in `tests/test_peers_config.py` first; run `uv run pytest tests/test_peers_config.py -q` and see them fail for the missing behaviour (not an import typo).
- Implement the smallest change that makes them pass; run `uv run pytest tests/test_peers_config.py -q` again and expect all passed.
- Run the task verify: `uv run pytest tests/test_peers_config.py tests/test_config.py tests/test_readme_config.py tests/test_examples.py -q && mise run lint`; expect all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

Defaults are design D2. The README row default column uses the JSON rendering test_readme_config.py expects. First README-editing task (README order: peers-config-section, peer-events-and-metrics, peer-membership-hooks, peer-messages-endpoint, peers-readme-guide.)
