"""End to end: resume after a service restart, and purge (D19), against the fake claude.

Uses the harness of `test_e2e_lifecycle` (real `run_service`, real HTTP, real `ccw-hook` relay).
"""

from __future__ import annotations

import json
from pathlib import Path

from test_e2e_lifecycle import E2E, e2e  # noqa: F401 - the fixture is used by name

from claude_code_wrapper.session import PROJECT_DIR_NAME


def interactive_argvs(fake_log: Path) -> list[list[str]]:
    """argv of every interactive (non-subcommand) fake claude invocation, in order."""
    out = []
    for line in fake_log.read_text().splitlines():
        argv = json.loads(line)["argv"]
        if argv[:1] != ["plugin"]:
            out.append(argv)
    return out


def flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


async def test_resume_after_service_restart(e2e: E2E) -> None:  # noqa: F811
    first = await e2e.start()
    created = await e2e.create(name="resumable")
    sid, claude_sid = created["session_id"], created["claude_session_id"]
    tid = await e2e.submit(sid, "first turn before the restart")
    assert (await e2e.finished(sid, tid))["state"] == "completed"
    transcript = (
        e2e.sessions_root / sid / "config" / "projects" / PROJECT_DIR_NAME / f"{claude_sid}.jsonl"
    )
    before = transcript.read_text().splitlines()
    assert any("first turn before the restart" in line for line in before)
    await e2e.stop(first)

    await e2e.start()  # a NEW service on the same sessions_root
    listed = (await e2e.call("GET", "/v1/sessions")).json()["sessions"]
    assert [(s["session_id"], s["state"]) for s in listed] == [(sid, "stopped")]
    stopped = (await e2e.call("GET", "/v1/sessions", params={"state": "stopped"})).json()
    assert [s["session_id"] for s in stopped["sessions"]] == [sid]

    resumed = await e2e.resume(sid)
    assert resumed["state"] == "ready"
    assert resumed["claude_session_id"] == claude_sid

    argvs = interactive_argvs(e2e.fake_log)
    assert len(argvs) == 2
    assert flag(argvs[0], "--session-id") == claude_sid and "--resume" not in argvs[0]
    assert flag(argvs[1], "--resume") == claude_sid and "--session-id" not in argvs[1]

    on_resume = await e2e.hook("onResume", lambda p: p["session_id"] == sid)
    assert on_resume["claude_session_id"] == claude_sid
    assert on_resume["data"]["state"] == "ready"
    assert len(e2e.hooks("onCreate", lambda p: p["session_id"] == sid)) == 1

    tid2 = await e2e.submit(sid, "second turn after the resume")
    assert (await e2e.finished(sid, tid2))["state"] == "completed"
    after = transcript.read_text().splitlines()
    assert after[: len(before)] == before, "the resumed session rewrote its transcript"
    assert any("second turn after the resume" in line for line in after[len(before) :])
    assert sorted(p.name for p in transcript.parent.glob("*.jsonl")) == [f"{claude_sid}.jsonl"]

    served = (await e2e.call("GET", f"/v1/sessions/{sid}/transcript")).text.splitlines()
    assert served == after
    turns = (await e2e.call("GET", f"/v1/sessions/{sid}/turns")).json()["turns"]
    assert [t["turn_id"] for t in turns] == [tid, tid2]


async def test_purge_removes_files(e2e: E2E) -> None:  # noqa: F811
    await e2e.start()
    created = await e2e.create(name="purged")
    sid = created["session_id"]
    assert created["workdir_mode"] == "empty"
    workdir = Path(created["workdir"])
    session_dir = e2e.sessions_root / sid
    assert workdir.is_dir() and workdir.is_relative_to(e2e.workdir_root)
    assert (session_dir / "session.json").is_file()
    tid = await e2e.submit(sid, "a turn before the purge")
    assert (await e2e.finished(sid, tid))["state"] == "completed"

    kept = await e2e.create(name="kept")
    deleted = (await e2e.call("DELETE", f"/v1/sessions/{sid}", params={"purge": "true"})).json()
    assert deleted["state"] == "stopped" and deleted["purged"] is True
    await e2e.hook("onDestroy", lambda p: p["session_id"] == sid)
    assert not session_dir.exists()
    assert not workdir.exists()
    await e2e.call("GET", f"/v1/sessions/{sid}", 404)

    # a plain DELETE keeps everything
    kept_id = kept["session_id"]
    plain = (await e2e.call("DELETE", f"/v1/sessions/{kept_id}")).json()
    assert plain["state"] == "stopped" and "purged" not in plain
    assert (e2e.sessions_root / kept_id / "session.json").is_file()
    assert Path(kept["workdir"]).is_dir()
