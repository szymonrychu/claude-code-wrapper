---
title: PTY process
status: in-progress
repo: claude-code-wrapper
value: Reliable control of an interactive claude process
files: [src/claude_code_wrapper/ring.py, src/claude_code_wrapper/pty_proc.py, tests/test_ring.py, tests/test_pty_proc.py]
verify: "uv run pytest tests/test_ring.py tests/test_pty_proc.py -q && mise run lint"
depends_on: [fake-claude]
owns: [pty-process]
uses: [fake-claude]
model: opus
estimate: 60
actual: ""
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/claude-code-wrapper-microser--pty-process
branch: claude-code-wrapper-microser--pty-process
verified_head: ""
run: ""
provides: []
---

# PTY process

## Requirement

Build the PTY child process driver and the output ring (D1, D13) in `src/claude_code_wrapper/ring.py` and `src/claude_code_wrapper/pty_proc.py`.

Ring: `Ring(capacity=65536)` keeps the last `capacity` bytes of output. `append(data: bytes)`; `text()` returns the content decoded (errors replaced) with ANSI CSI, OSC and other ESC sequences removed and whitespace runs collapsed to one space; `contains(needle)` is case-insensitive and whitespace-insensitive against `text()`; `tail(n=800)` returns the last n characters of `text()`; `reset()` empties it.

PtyProcess (asyncio):
- `async start(argv, cwd, env, rows=50, cols=200, on_output=None, on_exit=None)`: open a pty pair, set the window size, spawn with `subprocess.Popen` (slave as stdin/stdout/stderr, `start_new_session=True`, `close_fds=True`), close the slave in the parent, set the master non-blocking, and register `loop.add_reader(master, ...)` that reads available bytes, appends them to `self.ring` and calls `on_output(bytes)`.
- EOF/EIO on read, or child exit, triggers `on_exit(returncode)` exactly once (await the returncode without blocking the loop).
- `async write(data: bytes)`: serialised by an `asyncio.Lock`; loops until all bytes are written, handling EAGAIN/partial writes; raises `PtyClosed` after close or child exit.
- `async paste(text: str)`: writes `ESC[200~` + text (with any ESC characters removed) + `ESC[201~` as one write. The CR is NOT part of paste (the session writes it separately after a delay).
- `async close(grace=5.0)`: idempotent; remove the reader; send SIGTERM to the process group; close the master fd; wait up to `grace` seconds; then SIGKILL the group; never raises when the process is already gone. This order (signal group, then close fd, then kill) is load-bearing on macOS.
- Attributes `pid`, `returncode`, `ring`, `alive`.

Settled by the builder check (binding):
- `async send_prompt(text, delay)`: acquires the write lock ONCE, writes the bracketed paste, sleeps `delay`, then writes `\r` as a second write, and releases the lock; no other writer can interleave. Session submit and probe use only this.

## Acceptance conditions

- AC1: Ring text strips ANSI sequences and collapses whitespace [evidence: test:tests/test_ring.py::test_ring_strips_ansi_and_whitespace]
- AC2: Ring keeps only the newest capacity bytes [evidence: test:tests/test_ring.py::test_ring_capacity_drops_oldest]
- AC3: Ring reset empties it and contains is case and whitespace insensitive [evidence: test:tests/test_ring.py::test_ring_reset_and_contains]
- AC4: on_output receives child output bytes [evidence: test:tests/test_pty_proc.py::test_output_callback_receives_bytes]
- AC5: write round-trips through /bin/cat [evidence: test:tests/test_pty_proc.py::test_write_round_trip_cat]
- AC6: paste wraps text in bracketed paste markers without a CR [evidence: test:tests/test_pty_proc.py::test_paste_wraps_bracketed]
- AC7: on_exit fires once with the child's return code [evidence: test:tests/test_pty_proc.py::test_exit_callback_returncode]
- AC8: close terminates a TERM-ignoring child via SIGKILL within grace plus one second [evidence: test:tests/test_pty_proc.py::test_close_terminates_group_then_kill]
- AC9: close is idempotent and safe after exit [evidence: test:tests/test_pty_proc.py::test_close_idempotent]
- AC10: The fake claude boots under PtyProcess and its dialog is visible in the ring [evidence: test:tests/test_pty_proc.py::test_fake_claude_boots_under_pty]
- AC11: write after close raises PtyClosed [evidence: test:tests/test_pty_proc.py::test_write_after_close_raises]

- AC12: send_prompt writes paste and CR as two writes under one lock hold [evidence: test:tests/test_pty_proc.py::test_send_prompt_two_writes_under_one_lock]

## Files

- `src/claude_code_wrapper/ring.py`
- `src/claude_code_wrapper/pty_proc.py`
- `tests/test_ring.py`
- `tests/test_pty_proc.py`

## Interfaces

- `claude_code_wrapper.ring.Ring`; `claude_code_wrapper.pty_proc.PtyProcess`, `PtyClosed`. Consumed by session-boot (the only spawner).

## Expected outcome

The session layer can spawn claude, see its screen as clean text, write keystrokes, and stop it reliably on both macOS and Linux.

## Steps

- Write `tests/test_ring.py`; red; implement ring.py; green.
- Write `tests/test_pty_proc.py` with `/bin/cat`, `sh -c 'exit 3'`, `sh -c 'trap "" TERM; sleep 60'` and the `fake_claude_bin` fixture; red; implement pty_proc.py; green.
- `uv run pytest tests/test_ring.py tests/test_pty_proc.py -q` then `mise run lint`.

## Notes

- Sources: magic `M/pty_proc.py:73-95, 122-162` (close order, spawn), `M/ring.py`; tatara `W/internal/session/ring.go`.
- Use asyncio's loop reader, not a thread, so there is no reader thread to deadlock on fd close.
- Tests must not leave processes behind: close every PtyProcess in a fixture finalizer.
