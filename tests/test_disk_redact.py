"""Claude's on-disk session material is redacted once claude stops, and the dir is marked.

The config dir (`<session dir>/config`) holds claude's transcripts, file-history, debug logs
and more. After a stop, and on a service start for every dir a previous process left
unmarked, every text file there is rewritten through the service redactor and
`<session dir>/.redacted` is written. A resume clears the marker and works on the masked text.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from conftest import World, wait_turn, wait_until
from test_secret_env import BAR, FOO, _scenario, _session
from test_terminals import base_service_env

from claude_code_wrapper import __version__
from claude_code_wrapper.disk_redact import (
    MARKER_FILE,
    MARKER_FORMAT,
    is_marked,
    redact_session_dir,
    redact_text,
)
from claude_code_wrapper.manager import SessionManager
from claude_code_wrapper.redact import Redactor

PEM_LIKE = "CANARY-line-one\nCANARY-line-two"


def _redactor() -> Redactor:
    r = Redactor()
    r.add([FOO, BAR, 'CANARY-q"uote\\val', PEM_LIKE], min_len=1)
    return r


def _plant(config: Path, claude_sid: str = "abc") -> dict[str, Path]:
    """Claude-like files holding the values, plus files that must stay as they are."""
    files = {
        "file_history": config / "file-history" / claude_sid / "0123abcd@v1",
        "debug": config / "debug" / f"{claude_sid}.txt",
        "todos": config / "todos" / f"{claude_sid}-agent.json",
        "subagent": config / "projects" / "p" / claude_sid / "subagents" / "agent-1.jsonl",
        "credentials": config / ".credentials.json",
        "plugin": config / "plugins" / "repo" / "README.md",
        "binary": config / "debug" / "blob.bin",
    }
    for path in files.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    files["file_history"].write_text(f"API={FOO}\nother line\n")
    files["debug"].write_text(f"[DEBUG] env BAR={BAR} x\n")
    files["todos"].write_text(json.dumps([{"content": f"use {FOO}", "status": "pending"}]))
    files["subagent"].write_text(
        json.dumps({"uuid": "u-1", "message": {"content": f"tool said {FOO}"}}) + "\n"
    )
    files["credentials"].write_text(json.dumps({"token": FOO}))
    files["plugin"].write_text(f"docs {FOO}\n")
    files["binary"].write_bytes(b"\0\1" + FOO.encode() + b"\0")
    os.chmod(files["debug"], 0o640)
    return files


# ------------------------------------------------------------------ unit


def test_json_values_redacted_with_escapes_and_ids_kept() -> None:
    r = _redactor()
    line = json.dumps(
        {
            "uuid": f"id-{BAR}",
            "parentUuid": None,
            "timestamp": "2026-11-09T00:00:00Z",
            "message": {"content": [{"type": "text", "text": 'x CANARY-q"uote\\val y'}]},
            "toolUseResult": {"stdout": f"{PEM_LIKE}\n{BAR}"},
            "env": {FOO: "as a key"},
        },
        separators=(",", ":"),
    )
    out = redact_text(line, r)
    doc = json.loads(out)
    assert doc["uuid"] == f"id-{BAR}"  # identifiers are never rewritten
    assert doc["message"]["content"][0]["text"] == "x *** y"
    assert doc["toolUseResult"]["stdout"] == "***\n***"
    assert doc["env"] == {"***": "as a key"}
    assert redact_text('{"a": "plain"}', r) == '{"a": "plain"}'  # untouched, byte for byte
    assert redact_text(f"text {FOO} {{ not json", r) == "text *** { not json"


def test_redact_session_dir_masks_marks_and_skips(tmp_path: Path) -> None:
    session_dir = tmp_path / "ses_000000000001"
    config = session_dir / "config"
    files = _plant(config)
    transcript = config / "projects" / "p" / "abc.jsonl"
    transcript.write_text(
        json.dumps({"message": {"content": f"a {FOO}"}})
        + "\n"
        + json.dumps({"message": {"content": "clean"}})
        + "\n"
    )
    clean = config / "projects" / "p" / "clean.jsonl"
    clean.write_text('{"a": 1}\n')
    clean_mtime = clean.stat().st_mtime_ns
    outside = session_dir / "hook-state" / "x.txt"
    outside.parent.mkdir(parents=True)
    outside.write_text(FOO)
    debug_mtime = files["debug"].stat().st_mtime_ns

    stats = redact_session_dir(session_dir, _redactor())

    lines = transcript.read_text().splitlines()
    assert json.loads(lines[0]) == {"message": {"content": "a ***"}}
    assert lines[1] == json.dumps({"message": {"content": "clean"}})
    for name in ("file_history", "debug", "todos", "subagent"):
        text = files[name].read_text()
        assert FOO not in text and BAR not in text, name
        assert "***" in text, name
    assert files["file_history"].read_text() == "API=***\nother line\n"
    assert json.loads(files["todos"].read_text())[0]["content"] == "use ***"
    # mode and timestamps survive the rewrite
    assert files["debug"].stat().st_mode & 0o777 == 0o640
    assert files["debug"].stat().st_mtime_ns == debug_mtime
    # left alone: claude's login, installed plugins, binaries, files outside config
    assert FOO in files["credentials"].read_text()
    assert FOO in files["plugin"].read_text()
    assert files["binary"].read_bytes() == b"\0\1" + FOO.encode() + b"\0"
    assert outside.read_text() == FOO
    assert clean.stat().st_mtime_ns == clean_mtime  # unchanged files are not rewritten
    assert stats.skipped == 1 and stats.errors == 0
    assert not list(config.rglob("*.redact-tmp"))

    marker = json.loads((session_dir / MARKER_FILE).read_text())
    assert set(marker) == {"format", "wrapper_version", "redacted_at"}
    assert marker["format"] == MARKER_FORMAT and marker["wrapper_version"] == __version__
    assert is_marked(session_dir)


def test_non_utf8_text_is_skipped(tmp_path: Path) -> None:
    session_dir = tmp_path / "ses_000000000002"
    latin = session_dir / "config" / "debug" / "latin1.txt"
    latin.parent.mkdir(parents=True)
    latin.write_bytes(b"caf\xe9 " + FOO.encode())
    stats = redact_session_dir(session_dir, _redactor())
    assert stats.skipped == 1 and latin.read_bytes() == b"caf\xe9 " + FOO.encode()
    assert is_marked(session_dir)


def test_stale_marker_format_is_not_current(tmp_path: Path) -> None:
    (tmp_path / MARKER_FILE).write_text(json.dumps({"format": MARKER_FORMAT - 1}))
    assert not is_marked(tmp_path)
    (tmp_path / MARKER_FILE).write_text("not json")
    assert not is_marked(tmp_path)


def test_log_lines_carry_no_content(tmp_path: Path, caplog) -> None:
    session_dir = tmp_path / "ses_000000000003"
    _plant(session_dir / "config")
    caplog.set_level("DEBUG")
    redact_session_dir(session_dir, _redactor())
    assert FOO not in caplog.text and BAR not in caplog.text


# ------------------------------------------------------------------ manager


def _transcript(world: World, sid: str) -> Path:
    [path] = (world.session_dir(sid) / "config" / "projects").rglob("*.jsonl")
    return path


async def test_stop_redacts_config_dir_and_resume_still_works(world: World, tmp_path: Path) -> None:
    m, sid = await _session(world, tmp_path)
    await m.router(sid).submit_turn("show me")
    await wait_turn(m, sid)
    session_dir = world.session_dir(sid)
    transcript = _transcript(world, sid)
    assert FOO in transcript.read_text()  # claude really wrote it
    assert not (session_dir / MARKER_FILE).exists()
    files = _plant(session_dir / "config", m.get(sid)["claude_session_id"])

    await m.destroy(sid)

    assert FOO not in transcript.read_text() and BAR not in transcript.read_text()
    for line in transcript.read_text().splitlines():
        json.loads(line)  # still JSON lines
    for name in ("file_history", "debug", "todos", "subagent"):
        assert FOO not in files[name].read_text() and BAR not in files[name].read_text()
    assert FOO in files["credentials"].read_text()
    assert files["binary"].read_bytes() == b"\0\1" + FOO.encode() + b"\0"
    assert is_marked(session_dir)

    # resume on the masked transcript: the marker goes while claude runs, the session works
    resumed = await m.resume(sid)
    assert resumed["state"] == "ready"
    assert not (session_dir / MARKER_FILE).exists()
    turn = await m.router(sid).submit_turn("show me again")
    await wait_until(lambda: m.open_turns(sid).get(turn.turn_id).state == "completed")
    await m.destroy(sid)
    assert FOO not in transcript.read_text()
    assert is_marked(session_dir)


async def test_start_redacts_dirs_a_previous_process_left(world: World, tmp_path: Path) -> None:
    m1, sid = await _session(world, tmp_path)
    await m1.router(sid).submit_turn("show me")
    await wait_turn(m1, sid)
    claude_sid = m1.get(sid)["claude_session_id"]
    await m1.shutdown()
    session_dir = world.session_dir(sid)
    # As if the process had been killed before it redacted: raw material, no marker.
    (session_dir / MARKER_FILE).unlink()
    transcript = _transcript(world, sid)
    with transcript.open("a") as fh:
        fh.write(json.dumps({"type": "user", "message": {"content": f"raw {FOO}"}}) + "\n")
    files = _plant(session_dir / "config", claude_sid)
    meta = world.meta(sid)
    meta["state"] = "busy"
    (session_dir / "session.json").write_text(json.dumps(meta))

    service_env = {**base_service_env(), "FOO": FOO, "BAR": BAR}
    cfg = world.cfg(
        _scenario(), claude={"passthrough_env": ["FOO", "BAR"], "secret_env": ["FOO", "BAR"]}
    )
    m2 = SessionManager(cfg, internal_url=world.url, service_env=service_env, redactor=Redactor())
    world.managers.append(m2)
    await m2.load_persisted()

    assert FOO not in transcript.read_text()
    for name in ("file_history", "debug", "todos", "subagent"):
        assert FOO not in files[name].read_text() and BAR not in files[name].read_text()
    assert files["binary"].read_bytes() == b"\0\1" + FOO.encode() + b"\0"
    assert is_marked(session_dir)
    assert m2.get(sid)["state"] == "stopped"
