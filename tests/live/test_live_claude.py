"""Real Claude Code in the built image against the managed-settings baseline (spec Q2).

Each case drives a real claude session through the wrapper API; the model is the fake
Messages API, scripted by LIVE-CASE markers (tests/live/fake_messages_api.py).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

import pytest

from .conftest import (
    MANAGED_DIR,
    MANAGED_MOD_MARKER,
    SETTINGS_HOOK_LOG,
    STATUSLINE_MARKER,
    UNMANAGED_MOD_MARKER,
    Live,
    container_file,
    in_container,
    live_prompt,
    tool_results,
    wait_for,
)

pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parents[2]
MANAGED_FILE = f"{MANAGED_DIR}/managed-settings.json"
FENCE_FILE = "/home/agent/ccw-live-fence.txt"
#: The deny rule's own refusal; the write fence words its refusal differently.
DENY_WORDING = re.compile(r"denied", re.I)
#: N-385: a dangerous rm waits on a 120 s auto-deny countdown in 2.1.323; the turn must end.
DANGEROUS_RM_BOUND = 200.0


def _dockerfile_claude_version() -> str:
    match = re.search(
        r"^ARG CLAUDE_CODE_VERSION=(\S+)$", (ROOT / "Dockerfile").read_text(), re.MULTILINE
    )
    assert match, "Dockerfile has no ARG CLAUDE_CODE_VERSION"
    return match.group(1)


#: Lists (pid, ppid, args) from /proc: the image has no procps.
PROC_SCRIPT = """
import json, os
out = []
for pid in filter(str.isdigit, os.listdir('/proc')):
    try:
        stat = open(f'/proc/{pid}/stat').read()
        args = open(f'/proc/{pid}/cmdline', 'rb').read().split(b'\\0')
    except OSError:
        continue
    ppid = int(stat.rsplit(')', 1)[1].split()[1])
    out.append([int(pid), ppid, [a.decode(errors='replace') for a in args if a]])
print(json.dumps(out))
"""


def _processes() -> list[tuple[int, int, str]]:
    rows = json.loads(in_container("python3", "-c", PROC_SCRIPT).stdout)
    return [(pid, ppid, " ".join(args)) for pid, ppid, args in rows if args]


def _claude_pids() -> list[int]:
    return [
        pid
        for pid, _, args in _processes()
        if Path(args.split()[0]).name in ("claude", "claude.exe")
    ]


def _ancestors(pid: int, parents: dict[int, int]) -> list[int]:
    chain: list[int] = []
    node = pid
    while node in parents and node not in chain:
        chain.append(node)
        node = parents[node]
    return chain


def _ccw_pid(procs: list[tuple[int, int, str]]) -> int:
    ccw = [pid for pid, _, args in procs if re.search(r"\bccw serve\b", args)]
    assert len(ccw) == 1, procs
    return ccw[0]


def _result(case: str) -> dict[str, Any]:
    results = tool_results(case)
    assert len(results) == 1, (case, results)
    return results[0]


def _exists(path: str) -> bool:
    return in_container("test", "-e", path, check=False).returncode == 0


def test_live_claude_version_matches_dockerfile() -> None:
    out = in_container("claude", "--version").stdout.strip()
    assert out.startswith(_dockerfile_claude_version()), out


def test_managed_state_active_and_file_mode(live: Live) -> None:
    assert live.api.get("/readyz").json()["managed_settings"] == "active"
    assert in_container("stat", "-c", "%a %U %G", MANAGED_FILE).stdout.strip() == "644 agent agent"


def test_managed_relay_runs_before_managed_mod(live: Live, mod_turns: dict[str, Any]) -> None:
    """The managed PreToolUse relay saw the Bash call the managed mod then answered."""
    assert mod_turns["mod"]["state"] == "completed", mod_turns["mod"]
    sid = mod_turns["session"]["session_id"]
    calls = [
        e["data"]
        for e in live.events(sid, "tool_call")
        if e["data"].get("tool_name") == "Bash"
        and e["data"].get("tool_input", {}).get("command") == "echo live-mod"
    ]
    assert calls, "no relay-driven tool_call for the Bash call the mod answered"
    marker = container_file(MANAGED_MOD_MARKER)
    assert marker is not None and "echo live-mod" in marker
    result = _result("bash-mod")
    assert "live-managed-mod answered: echo live-mod" in result["content"], result


def test_settings_json_pretooluse_skipped_by_mod(mod_turns: dict[str, Any]) -> None:
    """Ruling R10: the settings.json hook fires for a call the mod passes on (control), the mod
    marker proves mods load, and the hook never saw the call the mod answered."""
    assert mod_turns["plain"]["state"] == "completed", mod_turns["plain"]
    log = container_file(SETTINGS_HOOK_LOG) or ""
    commands = [
        json.loads(line).get("tool_input", {}).get("command")
        for line in log.splitlines()
        if line.strip()
    ]
    assert "echo live-plain" in commands, "control: the settings.json PreToolUse hook never ran"
    assert container_file(MANAGED_MOD_MARKER) is not None, "control: the managed mod never ran"
    assert "echo live-mod" not in commands


def test_unmanaged_mod_not_loaded(mod_turns: dict[str, Any]) -> None:
    """Ruling R10: mods do load in this run (managed marker), yet the --plugin-dir mod, whose
    session.start and tool.call hooks would both write its marker, never ran."""
    assert container_file(MANAGED_MOD_MARKER) is not None, "control: the managed mod never ran"
    assert container_file(UNMANAGED_MOD_MARKER) is None


def test_tui_in_wrapper_pty_no_daemon(session: dict[str, Any]) -> None:
    procs = _processes()
    parents = {pid: ppid for pid, ppid, _ in procs}
    ccw = _ccw_pid(procs)
    claude = _claude_pids()
    assert claude, procs
    for pid in claude:
        assert ccw in _ancestors(pid, parents), (pid, _ancestors(pid, parents))
    assert not [args for _, _, args in procs if re.search(r"daemon|supervisor", args, re.I)]
    logs = in_container(
        "sh", "-c", "ls /home/agent/.claude/daemon.log /sessions/*/config/daemon.log", check=False
    )
    assert logs.returncode != 0 and not logs.stdout.strip(), logs.stdout


def test_write_to_managed_dir_denied(live: Live, session: dict[str, Any]) -> None:
    """The managed dir is inside the fence for this run (conftest OVERLAY), so only the managed
    deny rule can refuse this Write (ruling R9)."""
    target = f"{MANAGED_DIR}/x.json"
    turn = live.turn(
        session["session_id"],
        live_prompt("write-managed", "Write", {"file_path": target, "content": "{}"}),
    )
    assert turn["state"] == "completed", turn
    result = _result("write-managed")
    assert result["is_error"], result
    assert DENY_WORDING.search(result["content"]), result
    assert not _exists(target)


def test_write_outside_fence_refused(live: Live, session: dict[str, Any]) -> None:
    sid = session["session_id"]
    turn = live.turn(
        sid, live_prompt("write-home", "Write", {"file_path": FENCE_FILE, "content": "x"})
    )
    assert turn["state"] == "completed", turn
    result = _result("write-home")
    assert result["is_error"], result
    assert not _exists(FENCE_FILE)
    inside = f"{session['workdir']}/ccw-live-inside.txt"
    turn = live.turn(
        sid, live_prompt("write-workdir", "Write", {"file_path": inside, "content": "x"})
    )
    assert turn["state"] == "completed", turn
    assert not _result("write-workdir")["is_error"]
    assert _exists(inside)


def test_bash_tamper_restored_with_event(live: Live, session: dict[str, Any]) -> None:
    sid = session["session_id"]
    expected = live.api.get("/v1/managed-settings").json()["sha256"]
    seen = {e["seq"] for e in live.events(sid, "managed_settings_restored")}
    in_container("sh", "-c", f"echo '{{}}' > {MANAGED_FILE}")
    turn = live.turn(sid, live_prompt("tamper"))
    assert turn["state"] == "completed", turn
    events = [e for e in live.events(sid, "managed_settings_restored") if e["seq"] not in seen]
    assert any(e["data"]["trigger"] == "turn" and e["data"]["restored"] for e in events), events
    found = in_container("sha256sum", MANAGED_FILE).stdout.split()[0]
    assert found == expected
    content = container_file(MANAGED_FILE) or ""
    assert hashlib.sha256(content.encode()).hexdigest() == expected


def test_claude_env_has_managed_vars(session: dict[str, Any]) -> None:
    procs = _processes()
    parents = {pid: ppid for pid, ppid, _ in procs}
    ccw = _ccw_pid(procs)
    pids = [pid for pid in _claude_pids() if ccw in _ancestors(pid, parents)]
    assert pids
    wanted = ("CLAUDE_CODE_DISABLE_AGENT_VIEW", "CLAUDE_CODE_EXPERIMENTAL_CODE_MODE")
    for pid in pids:
        # Only the two names are read out of the environ; it is never printed whole.
        script = f"tr '\\0' '\\n' < /proc/{pid}/environ | grep -E '^({'|'.join(wanted)})=' || true"
        found = dict(
            line.split("=", 1) for line in in_container("sh", "-c", script).stdout.splitlines()
        )
        assert found == {
            "CLAUDE_CODE_DISABLE_AGENT_VIEW": "1",
            "CLAUDE_CODE_EXPERIMENTAL_CODE_MODE": "0",
        }, (pid, sorted(found))


def test_bash_rm_turn_completes(live: Live, session: dict[str, Any]) -> None:
    """N-385: a plain rm in a bypass session runs without a dialog and the turn ends."""
    target = f"{session['workdir']}/ccw-live-rm-me.txt"
    in_container("touch", target)
    started = time.monotonic()
    turn = live.turn(
        session["session_id"],
        live_prompt("bash-rm", "Bash", {"command": f"rm {target}", "description": "x"}),
        timeout=60,
    )
    assert turn["state"] == "completed", turn
    assert time.monotonic() - started < 60
    assert not _result("bash-rm")["is_error"]
    assert not _exists(target)


def test_dangerous_rm_turn_completes(live: Live) -> None:
    """N-385: an rm Claude Code's safety check holds (here the session's own workdir) shows a
    dialog with an auto-deny countdown in bypass mode; the headless turn still ends, denied,
    within a bound, and leaves no dialog open."""
    session = live.create_session("live-rm")
    sid, workdir = session["session_id"], session["workdir"]
    started = time.monotonic()
    turn = live.turn(
        sid,
        live_prompt(
            "bash-rm-workspace", "Bash", {"command": f"rm -rf {workdir}", "description": "x"}
        ),
        timeout=DANGEROUS_RM_BOUND,
    )
    assert turn["state"] == "completed", turn
    assert time.monotonic() - started < DANGEROUS_RM_BOUND
    assert _result("bash-rm-workspace")["is_error"]
    assert _exists(workdir)
    state = wait_for(
        lambda: (s := live.api.get(f"/v1/sessions/{sid}").json())["state"] == "ready" and s,
        "session ready after the denied rm",
        30,
    )
    assert state["waiting"] is None, state


def test_statusline_relay_fires_with_managed_file(session: dict[str, Any]) -> None:
    """N-249: the baseline never sets allowManagedHooksOnly, so the statusLine relay (and the
    command chained behind it) still runs with the managed file active."""
    wait_for(lambda: container_file(STATUSLINE_MARKER), "statusLine chain marker", 60)
