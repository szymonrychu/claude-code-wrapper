---
title: Peers config section
status: todo
repo: .
value: "Operators turn peer messaging on with one validated config section; nothing changes when it is off"
files: [src/claude_code_wrapper/config.py, src/claude_code_wrapper/data/config.example.yaml, README.md, tests/test_config_peers.py]
verify: "uv run pytest tests/test_config_peers.py tests/test_readme_config.py tests/test_config.py tests/test_examples.py -q && mise run lint"
depends_on: []
owns: [peers-config]
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

# Peers config section

## Requirement

An opt-in `peers:` config section, disabled by default, validated at config load (design D2). Owns part `peers-config` (src/claude_code_wrapper/config.py), registered as the cross-epic contract `peers-config`.

- `class PeersConfig(_Model)` in src/claude_code_wrapper/config.py with exactly these fields:
  - `enabled: bool = False`
  - `dir: str = "/peers"` - absolute (reuse `_absolute`, config.py:66), no `..` path component; the same path must be mounted in every container.
  - `name: str = ""` - the instance name; empty means the host name at runtime (resolved by the peers module, not here). Must match `^[A-Za-z0-9._-]*$` and be at most 64 characters.
  - `probe_timeout_ms: int = Field(250, ge=1, le=10000)` - liveness connect probe timeout (250 mirrors Claude's own probe, fact F4).
- Module constants in config.py: `PEER_SOCKET_MAX_BYTES = 103` (Claude's limit, fact F2) and `SESSION_ID_LEN = 16` (`"ses_"` plus 12 hex, manager.py `_new_session_id`).
- Socket path length check at load, always (enabled or not): `len(f"{dir}/socks/{'x' * SESSION_ID_LEN}.sock".encode())` must be `<= PEER_SOCKET_MAX_BYTES`; otherwise a `ConfigError` line at `peers.dir` naming the byte count and the 103 limit. A dir giving exactly 103 bytes loads; one byte more fails.
- `Config.peers: PeersConfig = Field(default_factory=PeersConfig)`, placed after `limits`.
- Cross-field rules on `Config` (model validator), only when `peers.enabled`:
  - `claude.extra_args` containing `--messaging-socket-path` or `--messaging-socket-path=...` is an error at `claude.extra_args`: "ccw sets --messaging-socket-path when peers.enabled".
  - `claude.mcp_servers.ccw` is reserved: extend `_ccw_name_reserved` (config.py:602) so the condition is `sessions.commands.agent or peers.enabled`, same message.
- With `peers.enabled: false` every existing config loads and validates exactly as before.
- README `## Configuration reference` gains `### \`peers\`` with one table row per key (`peers.enabled`, `peers.dir`, `peers.name`, `peers.probe_timeout_ms`) in the existing `Key | Type | Default | Env var | Description` format, defaults rendered as tests/test_readme_config.py `render_default` expects, Env var `-`. The descriptions state the 103-byte socket path limit for `dir` and the host-name default for `name`.
- src/claude_code_wrapper/data/config.example.yaml gains a `peers:` section after `limits:` with every key at its default and a one-line comment each (`name: ${CCW_PEERS_NAME:-}` is acceptable since it interpolates to the default); the file still loads with no environment set.

Interfaces: `claude_code_wrapper.config.PeersConfig`, `Config.peers`, `PEER_SOCKET_MAX_BYTES`, `SESSION_ID_LEN`.

Notes: config errors are formatted by `_format_errors` (config.py:631) with `_at(sub, message)` for nested locations; follow the `_auto_compact_before_reaper` pattern. tests/test_readme_config.py enforces a row for every leaf key and a `### \`peers\`` heading for the new top-level field.

## Acceptance conditions

- AC1: Config().peers is disabled with dir /peers, empty name and probe_timeout_ms 250 [evidence: test:tests/test_config_peers.py::test_peers_defaults_disabled]
- AC2: a dir whose socket path is exactly 103 bytes loads and one byte longer fails at peers.dir naming the limit [evidence: test:tests/test_config_peers.py::test_socket_path_length_limit]
- AC3: a relative dir or one with a .. component is rejected at peers.dir [evidence: test:tests/test_config_peers.py::test_dir_must_be_absolute]
- AC4: an instance name outside [A-Za-z0-9._-] or over 64 characters is rejected [evidence: test:tests/test_config_peers.py::test_instance_name_pattern]
- AC5: probe_timeout_ms below 1 or above 10000 is rejected [evidence: test:tests/test_config_peers.py::test_probe_timeout_bounds]
- AC6: --messaging-socket-path in claude.extra_args fails when peers.enabled and loads when disabled [evidence: test:tests/test_config_peers.py::test_extra_args_socket_flag_rejected_when_enabled]
- AC7: claude.mcp_servers.ccw is rejected when peers.enabled even with an empty agent allowlist [evidence: test:tests/test_config_peers.py::test_ccw_mcp_name_reserved_when_enabled]
- AC8: README documents every peers key with the model default [evidence: command:uv run pytest tests/test_readme_config.py -q]
- AC9: the example config carries a peers section and every example config still loads [evidence: command:uv run pytest tests/test_examples.py tests/test_config.py tests/test_config_peers.py -q]

## Files

- `src/claude_code_wrapper/config.py`
- `src/claude_code_wrapper/data/config.example.yaml`
- `README.md`
- `tests/test_config_peers.py`

## Interfaces

As in the Requirement's Interfaces paragraph: `config.PeersConfig`, `Config.peers`, `PEER_SOCKET_MAX_BYTES`, `SESSION_ID_LEN` (contract `peers-config`).

## Expected outcome

`ccw check-config` accepts a valid peers section, rejects an over-long dir, a bad name or a conflicting extra arg with a line naming the key, and every existing config loads unchanged; README and the example config document every key.

## Steps

- Write the tests named in the acceptance conditions in `tests/test_config_peers.py` first; run `uv run pytest tests/test_config_peers.py -q` and see them fail for the missing behaviour (not an import typo).
- Implement the smallest change that makes them pass; run `uv run pytest tests/test_config_peers.py -q` and expect all passed.
- Run the task verify: `uv run pytest tests/test_config_peers.py tests/test_readme_config.py tests/test_config.py tests/test_examples.py -q && mise run lint`; expect pytest all passed and ruff clean.
- No retirement test in this task; a removal pin would assert on `git ls-files <path>`, never on the filesystem.

## Notes

See the Requirement's Notes paragraph.
