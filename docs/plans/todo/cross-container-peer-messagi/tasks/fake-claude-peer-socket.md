---
title: Fake claude peer socket
status: todo
repo: .
value: Every peer test can run against a fake claude that really binds and serves the messaging socket
files: [tests/fake_claude/fake_claude.py, tests/conftest.py, tests/test_fake_claude_peers.py]
verify: "uv run pytest tests/test_fake_claude_peers.py tests/test_fake_claude.py tests/test_fake_claude_mcp.py -q && mise run lint"
depends_on: []
owns: [fake-peer-socket, peers-dir-fixture]
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

# Fake claude peer socket

## Requirement

The scripted fake claude learns Claude Code's `--messaging-socket-path` flag and binds a real AF_UNIX listener there, so every peer test (in-process and docker) runs without a real claude (design D8, facts F2 and F3). Owns parts `fake-peer-socket` (tests/fake_claude/fake_claude.py) and `peers-dir-fixture` (tests/conftest.py). This is the only task of the epic that edits tests/conftest.py.

- `--messaging-socket-path P` is a valued flag: add it to `VALUED_FLAGS` (tests/fake_claude/fake_claude.py:180-186) so `P` is never taken as a prompt positional, in interactive and print mode.
- Interactive mode with the flag: before the first idle footer is printed (so ccw's "ready" implies the socket is bound), bind an AF_UNIX SOCK_STREAM socket at `P`, chmod it 0600 and listen. If `P` already exists: connect to it; a refused or missing connection means stale, unlink and bind; an accepted connection means live, print `socket already live: P` to stderr and exit 1 (mirrors Claude's "must not already be live").
- A daemon thread accepts connections; each connection is read to EOF as NDJSON (lines split on `\n`, each at most 1048576 chars); every complete line is appended as one JSON line to `$CLAUDE_CONFIG_DIR/fake-peer-inbox.jsonl`: the decoded object when the line is JSON, else `{"raw": "<line>"}`. Appends are serialized with a lock so concurrent senders never interleave.
- `P` is unlinked on every exit path of the interactive fake (normal return, `Exit`, the idle 0x03 exit, `{"exit": n}` steps, SIGTERM and SIGHUP), using `atexit` plus the signal handlers.
- Without the flag nothing is bound and no inbox file is written (zero change for every existing test).
- `log_invocation` (fake_claude.py:960) also records `CLAUDE_CODE_HARBOR_KITE` and `CLAUDE_CODE_SESSION_NAME` in `env`.
- The module docstring documents the flag, the inbox file and the two new logged env names.
- tests/conftest.py gains a fixture `peers_dir` returning a fresh directory made by `tempfile.mkdtemp(prefix="ccwp-", dir="/tmp")` (mode 0700, owned by the test uid), removed with `shutil.rmtree(ignore_errors=True)` after the test. It lives under /tmp because pytest's macOS `tmp_path` is long enough to push `<dir>/socks/<session id>.sock` past the 103-byte limit (fact F2).
- New tests in tests/test_fake_claude_peers.py drive the fake under the `Pty` helper pattern of tests/test_fake_claude.py.

Interfaces: CLI `fake_claude ... --messaging-socket-path <abs path>`; inbox file `$CLAUDE_CONFIG_DIR/fake-peer-inbox.jsonl` (one JSON object per received line); pytest fixture `peers_dir -> pathlib.Path`.

Notes: stdlib only (the fake runs with `python -I`). Keep the existing scenario and hook behaviour byte-identical. The docker test (peers-two-container-test) reads the inbox file through `docker compose exec`, so the path must stay exactly as above.

## Acceptance conditions

- AC1: with --messaging-socket-path the fake binds a listening AF_UNIX socket at the path before the idle footer appears [evidence: test:tests/test_fake_claude_peers.py::test_socket_bound_before_footer]
- AC2: NDJSON lines sent by two clients land in fake-peer-inbox.jsonl, JSON decoded or as raw, never interleaved [evidence: test:tests/test_fake_claude_peers.py::test_inbound_lines_recorded]
- AC3: a stale socket file is replaced and a live one makes the fake exit 1 [evidence: test:tests/test_fake_claude_peers.py::test_stale_socket_replaced_live_socket_refused]
- AC4: the socket path is unlinked after a normal exit and after SIGTERM [evidence: test:tests/test_fake_claude_peers.py::test_socket_unlinked_on_exit]
- AC5: without the flag no socket and no inbox file exist, and the flag value is not taken as a prompt [evidence: test:tests/test_fake_claude_peers.py::test_no_flag_binds_nothing]
- AC6: the invocation log records CLAUDE_CODE_HARBOR_KITE and CLAUDE_CODE_SESSION_NAME [evidence: test:tests/test_fake_claude_peers.py::test_log_records_peer_env]
- AC7: the peers_dir fixture yields a 0700 dir under /tmp short enough for a 103-byte socket path [evidence: test:tests/test_fake_claude_peers.py::test_peers_dir_fixture_is_short_and_private]
- AC8: the existing fake claude suites stay green [evidence: command:uv run pytest tests/test_fake_claude.py tests/test_fake_claude_mcp.py tests/test_fake_claude_commands.py -q]

## Files

- `tests/fake_claude/fake_claude.py`
- `tests/conftest.py`
- `tests/test_fake_claude_peers.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

What is true and observable when the task is done.

## Steps

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
