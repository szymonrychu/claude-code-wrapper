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
    api_log,
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
#: After the denied rm: the 1 s ESC grace plus the 30 s settle of the permission lock, padded.
DIALOG_CLEAR_BOUND = 60.0


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
    # The relay check can restore the file before the turn-end check: any trigger counts.
    assert any(e["data"]["restored"] for e in events), events
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
    # The relayed PermissionRequest is resolved deny by the Stop; the wrapper then sends its
    # one guarded ESC and keeps the permission lock for ORPHAN_SETTLE_SECONDS (30 s).
    flow = "permission_requested,permission_resolved,dialog_auto_dismissed"
    try:
        wait_for(
            lambda: (
                (s := live.api.get(f"/v1/sessions/{sid}").json())["state"] == "ready"
                and s["waiting"] is None
            ),
            "session ready with nothing waiting after the denied rm",
            DIALOG_CLEAR_BOUND,
        )
    except AssertionError as exc:
        raise AssertionError(
            f"{exc}: {live.api.get(f'/v1/sessions/{sid}').json()['waiting']}, "
            f"{[(e['type'], e['data']) for e in live.events(sid, flow)]}"
        ) from None


def test_statusline_relay_fires_with_managed_file(session: dict[str, Any]) -> None:
    """N-249: the baseline never sets allowManagedHooksOnly, so the statusLine relay (and the
    command chained behind it) still runs with the managed file active."""
    wait_for(lambda: container_file(STATUSLINE_MARKER), "statusLine chain marker", 60)


# ---------------------------------------------------------------- pressure probes (T-423)

#: Tiny thresholds so the fake API's few input tokens already mean level 1; l2 and up stay far away.
PROBE_POLICY: dict[str, Any] = {
    "version": 1,
    "thresholds": {
        "l1_tokens": 1,
        "l2_tokens": 10_000_000,
        "l3_tokens": 20_000_000,
        "compact_tokens": 30_000_000,
        "l1_pct": 0.0001,
        "l2_pct": 90,
        "l3_pct": 95,
        "compact_pct": 99,
    },
    "profile": "work",
    "texts": {
        lv: {"full": f"LIVE-PRESSURE-{lv}", "short": f"LIVE-PRESSURE-{lv}"}
        for lv in ("l1", "l2", "l3")
    },
}
PROBE_BOUND = 90.0


def _push_policy(live: Live, sid: str) -> None:
    put = live.api.put(f"/v1/sessions/{sid}/pressure", json=PROBE_POLICY)
    assert put.status_code == 200, put.text


def test_live_post_tool_batch_is_emitted(live: Live) -> None:
    """The pinned claude emits PostToolBatch after a tool group: a pressure delivery at a tool
    boundary comes from that hook. The gauge needs a statusLine after the first API reply, so
    tool turns repeat until a delivery shows. If claude never emits PostToolBatch, the
    PostToolUse fallback must deliver instead (README "Context pressure"); the result is
    recorded in MEMORY.md."""
    session = live.create_session("live-batch")
    sid = session["session_id"]
    _push_policy(live, sid)
    hooks: set[str] = set()
    for n in range(4):
        turn = live.turn(
            sid,
            live_prompt(f"batch-{n}", "Bash", {"command": f"echo batch-{n}", "description": "x"}),
        )
        assert turn["state"] == "completed", turn
        hooks = {
            e["data"]["hook"]
            for e in live.events(sid, "pressure_delivered")
            if e["data"]["hook"] != "UserPromptSubmit"
        }
        if hooks:
            break
    assert hooks, "no tool-boundary pressure delivery: neither PostToolBatch nor PostToolUse"
    print(f"live-claude observed tool-boundary pressure hooks: {sorted(hooks)}")
    assert hooks == {"PostToolBatch"} or hooks == {"PostToolUse"}, hooks
    marker = [
        r for r in api_log() if "LIVE-PRESSURE-l1" in json.dumps(r["body"].get("messages", []))
    ]
    assert marker, "the injected text never reached the model request"


def test_live_stop_payload_carries_background_tasks(live: Live) -> None:
    """A run_in_background Bash is in the Stop payload's background_tasks: the inventory route
    (fed by every main-agent Stop) lists it."""
    session = live.create_session("live-bg")
    sid = session["session_id"]
    try:
        live.turn(
            sid,
            live_prompt(
                "bg-task",
                "Bash",
                {"command": "sleep 600", "description": "x", "run_in_background": True},
            ),
        )
        inflight = wait_for(
            lambda: (live.api.get(f"/v1/sessions/{sid}/inflight").json())["background_tasks"],
            "a background task in the Stop inventory",
            PROBE_BOUND,
        )
        assert inflight, inflight
    finally:
        live.api.delete(f"/v1/sessions/{sid}")


def test_live_clear_and_background_shell(live: Live) -> None:
    """Observe whether a background shell survives /clear (the orchestrator gate blocks the
    clear on running shells either way). The observation is printed and asserted only as
    'a definite answer'; MEMORY.md records what CI saw."""
    session = live.create_session("live-clear")
    sid = session["session_id"]
    marker = f"/tmp/ccw-live-bgshell-{sid}"
    command = f"sh -c 'while true; do date > {marker}; sleep 1; done'"
    try:
        live.turn(
            sid,
            live_prompt(
                "clear-bg",
                "Bash",
                {"command": command, "description": "x", "run_in_background": True},
            ),
        )
        wait_for(lambda: container_file(marker), "the background shell started", PROBE_BOUND)
        queued = live.api.post(f"/v1/sessions/{sid}/commands", json={"name": "clear"})
        assert queued.status_code == 202, queued.text
        cid = queued.json()["id"]
        wait_for(
            lambda: (
                live.api.get(f"/v1/sessions/{sid}/commands/{cid}").json()["state"]
                in ("completed", "failed")
            ),
            "the clear command finished",
            PROBE_BOUND,
        )
        first = container_file(marker)
        time.sleep(3)
        alive = container_file(marker) != first
        print(f"live-claude observed: background shell survives /clear = {alive}")
        assert isinstance(alive, bool)
    finally:
        live.api.delete(f"/v1/sessions/{sid}")
        in_container("rm", "-f", marker, check=False)
