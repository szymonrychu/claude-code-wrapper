"""Behavioural tests for the scripted fake claude (tests/fake_claude/fake_claude.py)."""

from __future__ import annotations

import json
import os
import pty
import re
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

FAKE = Path(__file__).parent / "fake_claude" / "fake_claude.py"
BASIC = Path(__file__).parent / "fixtures" / "scenarios" / "basic.json"
UUID_RE = re.compile(r"^[0-9a-f-]{36}$")


def run_cli(args, env, cwd):
    return subprocess.run(
        [sys.executable, "-I", str(FAKE), *args],
        env=env, cwd=cwd, capture_output=True, text=True, timeout=20, check=False,
    )


class Pty:
    """Drive the fake under a real PTY."""

    def __init__(self, args, env, cwd):
        self.master, slave = pty.openpty()
        self.proc = subprocess.Popen(
            [sys.executable, "-I", str(FAKE), *args],
            stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd, close_fds=True,
        )
        os.close(slave)
        self.buf = ""

    def pump(self, timeout=0.1):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            r, _, _ = select.select([self.master], [], [], 0.05)
            if r:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    return
                if not data:
                    return
                self.buf += data.decode(errors="replace")

    def wait_for(self, text, timeout=10, since=0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.pump(0.05)
            if text in self.buf[since:]:
                return
            if self.proc.poll() is not None:
                self.pump(0.1)
                if text in self.buf[since:]:
                    return
                break
        raise AssertionError(f"timeout waiting for {text!r}; got {self.buf!r}")

    def send(self, data: bytes):
        os.write(self.master, data)

    def wait_exit(self, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.pump(0.05)
            if self.proc.poll() is not None:
                self.pump(0.1)
                return self.proc.returncode
        raise AssertionError(f"did not exit; got {self.buf!r}")

    def boot(self):
        self.wait_for("Bypass Permissions mode")
        self.send(b"\x1b[B")
        time.sleep(0.05)
        self.send(b"\r")
        self.wait_for("? for shortcuts")

    def submit(self, prompt: str, wait="? for shortcuts"):
        since = len(self.buf)
        self.send(b"\x1b[200~" + prompt.encode() + b"\x1b[201~")
        time.sleep(0.1)
        self.send(b"\r")
        self.wait_for(wait, since=since)

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        os.close(self.master)


@pytest.fixture
def env(tmp_path, claude_config_dir):
    work = tmp_path / "work"
    work.mkdir()
    e = {
        "PATH": os.environ["PATH"],
        "CLAUDE_CONFIG_DIR": str(claude_config_dir),
        "FAKE_CLAUDE_SCENARIO": str(BASIC),
        "FAKE_CLAUDE_LOG": str(tmp_path / "log.jsonl"),
    }
    return e


@pytest.fixture
def work(tmp_path):
    return str(tmp_path / "work")


@pytest.fixture
def spawn(env, work):
    made = []

    def _spawn(args=(), **extra):
        p = Pty(list(args), {**env, **extra}, work)
        made.append(p)
        return p

    yield _spawn
    for p in made:
        p.close()


def transcript_lines(config_dir, work, sid=None):
    proj = re.sub(r"[^A-Za-z0-9]", "-", work)
    d = Path(config_dir) / "projects" / proj
    files = [d / f"{sid}.jsonl"] if sid else sorted(d.glob("*.jsonl"))
    return [json.loads(x) for x in files[0].read_text().splitlines()]


def hook_settings(config_dir, hooks):
    (Path(config_dir) / "settings.json").write_text(json.dumps({"hooks": hooks}))


def test_version(env, work):
    r = run_cli(["--version"], env, work)
    assert r.returncode == 0
    assert r.stdout.strip() == "0.0.0 (Fake Claude)"


def test_non_tty_exits_2(env, work):
    r = run_cli([], env, work)
    assert r.returncode == 2


def test_argv_logged(spawn, env, work):
    p = spawn(["--model", "m1", "--effort", "high", "--weird-flag"], CCW_SESSION_ID="s1",
              CCW_INTERNAL_URL="http://127.0.0.1:1", SECRET_TOKEN="hunter2")
    p.boot()
    p.send(b"\x03")
    line = json.loads(Path(env["FAKE_CLAUDE_LOG"]).read_text().splitlines()[0])
    assert line["argv"] == ["--model", "m1", "--effort", "high", "--weird-flag"]
    assert line["cwd"] == work
    assert line["env"]["CCW_SESSION_ID"] == "s1"
    assert line["env"]["CLAUDE_CONFIG_DIR"] == env["CLAUDE_CONFIG_DIR"]
    assert "hunter2" not in json.dumps(line)


def test_boot_banner_has_ansi_and_dialog(spawn):
    p = spawn()
    p.wait_for("Bypass Permissions mode")
    assert "\x1b[" in p.buf
    assert "1. No, exit" in p.buf
    assert "2. Yes, I accept" in p.buf


def test_cr_without_down_exits_1(spawn):
    p = spawn()
    p.wait_for("Bypass Permissions mode")
    p.send(b"\r")
    assert p.wait_exit() == 1


def test_input_before_min_seconds_exits_1(spawn):
    p = spawn()
    p.wait_for("Fake Claude")
    p.send(b"x")
    assert p.wait_exit() == 1


def test_no_bypass_dialog(spawn, scenario_file):
    sc = scenario_file({"boot": {"bypass_dialog": False, "min_seconds": 0.1}})
    p = spawn(FAKE_CLAUDE_SCENARIO=str(sc))
    p.wait_for("? for shortcuts")
    assert "Bypass Permissions mode" not in p.buf


def test_basic_turn_transcript_and_hooks(spawn, env, work, tmp_path):
    out = tmp_path / "hooks.out"
    cmd = f"cat >> {out}; echo >> {out}"
    hook_settings(env["CLAUDE_CONFIG_DIR"], {
        ev: [{"matcher": "", "hooks": [{"type": "command", "command": cmd, "timeout": 5}]}]
        for ev in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")
    })
    p = spawn(["--session-id", "11111111-1111-4111-8111-111111111111"])
    p.boot()
    p.submit("hello there")
    lines = transcript_lines(env["CLAUDE_CONFIG_DIR"], work)
    types = [x["type"] for x in lines]
    assert types == ["user", "assistant", "assistant", "user", "assistant"]
    assert lines[0]["message"]["content"] == "hello there"
    assert lines[0]["sessionId"] == "11111111-1111-4111-8111-111111111111"
    assert lines[0]["isSidechain"] is False and lines[0]["cwd"] == work
    assert lines[1]["message"]["content"][0]["type"] == "thinking"
    assert lines[2]["message"]["content"][0]["type"] == "tool_use"
    usage = lines[4]["message"]["usage"]
    assert set(usage) == {"input_tokens", "output_tokens", "cache_read_input_tokens",
                          "cache_creation_input_tokens"}
    tr = lines[3]["message"]["content"][0]
    assert tr["type"] == "tool_result" and tr["content"] == "hi" and tr["is_error"] is False
    assert tr["tool_use_id"] == lines[2]["message"]["content"][0]["id"]
    payloads = [json.loads(x) for x in out.read_text().splitlines()]
    assert [x["hook_event_name"] for x in payloads] == [
        "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]
    assert payloads[0]["prompt"] == "hello there"
    assert payloads[1]["tool_name"] == "Bash"
    assert payloads[1]["tool_input"] == {"command": "echo hi"}
    assert payloads[2]["tool_response"]
    assert payloads[3]["stop_hook_active"] is False
    assert payloads[3]["last_assistant_message"] == "All done."
    assert payloads[3]["transcript_path"].endswith(".jsonl")
    assert all(x["session_id"] == "11111111-1111-4111-8111-111111111111" for x in payloads)


def test_default_echo_turn(spawn, env, work, scenario_file):
    sc = scenario_file(
        {"boot": {"bypass_dialog": False}, "turns": [{"match": "^zzz$", "steps": []}]})
    p = spawn(FAKE_CLAUDE_SCENARIO=str(sc))
    p.wait_for("? for shortcuts")
    p.submit("ping")
    lines = transcript_lines(env["CLAUDE_CONFIG_DIR"], work)
    assert lines[-1]["message"]["content"][0]["text"] == "echo: ping"
    assert UUID_RE.match(lines[0]["sessionId"])


def test_cr_in_same_chunk_does_not_submit(spawn, env, work, scenario_file):
    sc = scenario_file({"boot": {"bypass_dialog": False}})
    p = spawn(FAKE_CLAUDE_SCENARIO=str(sc))
    p.wait_for("? for shortcuts")
    p.send(b"\x1b[200~abc\x1b[201~\r")
    p.pump(0.6)
    proj = Path(env["CLAUDE_CONFIG_DIR"]) / "projects"
    assert not list(proj.rglob("*.jsonl")) or all(
        not f.read_text().strip() for f in proj.rglob("*.jsonl"))
    since = len(p.buf)
    p.send(b"\r")
    p.wait_for("? for shortcuts", since=since)
    lines = transcript_lines(env["CLAUDE_CONFIG_DIR"], work)
    assert lines[0]["message"]["content"].strip() == "abc"


def test_resume_missing(env, work):
    r = run_cli(["--resume", "nope"], env, work)
    assert r.returncode == 1
    assert "No conversation found with session ID" in (r.stdout + r.stderr)


def test_resume_appends(spawn, env, work):
    sid = "22222222-2222-4222-8222-222222222222"
    p = spawn(["--session-id", sid])
    p.boot()
    p.submit("one")
    n = len(transcript_lines(env["CLAUDE_CONFIG_DIR"], work, sid))
    p.send(b"\x03")
    p.wait_exit()
    p2 = spawn(["-r", sid])
    p2.boot()
    p2.submit("two")
    assert len(transcript_lines(env["CLAUDE_CONFIG_DIR"], work, sid)) > n


def test_project_dir_name_override(spawn, env):
    p = spawn(CLAUDE_CODE_PROJECT_DIR_NAME="custom-proj")
    p.boot()
    p.submit("hi")
    assert list((Path(env["CLAUDE_CONFIG_DIR"]) / "projects" / "custom-proj").glob("*.jsonl"))


def test_interrupt_during_sleep(spawn, env, work, tmp_path, scenario_file):
    out = tmp_path / "stop.out"
    hook_settings(env["CLAUDE_CONFIG_DIR"], {"Stop": [{"matcher": "", "hooks": [
        {"type": "command", "command": f"cat >> {out}", "timeout": 5}]}]})
    sc = scenario_file({"boot": {"bypass_dialog": False},
                        "turns": [{"match": ".*", "steps": [{"sleep": 30}]}]})
    p = spawn(FAKE_CLAUDE_SCENARIO=str(sc))
    p.wait_for("? for shortcuts")
    since = len(p.buf)
    p.send(b"\x1b[200~go\x1b[201~")
    time.sleep(0.1)
    p.send(b"\r")
    time.sleep(0.4)
    p.send(b"\x1b")
    p.wait_for("[Request interrupted by user]", since=since)
    p.wait_for("? for shortcuts", since=since)
    lines = transcript_lines(env["CLAUDE_CONFIG_DIR"], work)
    assert lines[-1]["message"]["content"][0]["text"] == "[Request interrupted by user]"
    p.pump(0.3)
    assert not out.exists()


def test_print_and_exit_steps(spawn, scenario_file):
    sc = scenario_file({"boot": {"bypass_dialog": False}, "turns": [
        {"match": "fatal", "steps": [{"print": "API Error: boom"}]},
        {"match": "bye", "steps": [{"exit": 7}]}]})
    p = spawn(FAKE_CLAUDE_SCENARIO=str(sc))
    p.wait_for("? for shortcuts")
    p.submit("fatal", wait="API Error: boom")
    since = len(p.buf)
    p.send(b"\x1b[200~bye\x1b[201~")
    time.sleep(0.1)
    p.send(b"\r")
    assert p.wait_exit() == 7
    assert since <= len(p.buf)


def test_pre_tool_use_deny_and_additional_context(spawn, env, work, tmp_path, scenario_file):
    deny = tmp_path / "deny.sh"
    deny.write_text('cat >/dev/null; echo "blocked by policy" >&2; exit 2\n')
    ctx = tmp_path / "ctx.sh"
    ctx.write_text(
        'cat >/dev/null; echo \'{"hookSpecificOutput":{"additionalContext":"extra"}}\'\n')
    hook_settings(env["CLAUDE_CONFIG_DIR"], {
        "PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": f"sh {deny}"}]}],
        "PostToolUseFailure": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": f"sh {ctx}"}]}],
    })
    p = spawn()
    p.boot()
    p.submit("x")
    lines = transcript_lines(env["CLAUDE_CONFIG_DIR"], work)
    results = [c for x in lines if x["type"] == "user" and isinstance(x["message"]["content"], list)
               for c in x["message"]["content"]]
    assert results[0]["is_error"] is True and "blocked by policy" in results[0]["content"]
    assert any(x["type"] == "system" and "extra" in json.dumps(x) for x in lines)


def test_pre_tool_use_json_deny(spawn, env, work, tmp_path):
    h = tmp_path / "d.sh"
    h.write_text('cat >/dev/null; echo \'{"hookSpecificOutput":{"permissionDecision":"deny",'
                 '"permissionDecisionReason":"nope"}}\'\n')
    hook_settings(env["CLAUDE_CONFIG_DIR"], {"PreToolUse": [{"matcher": "", "hooks": [
        {"type": "command", "command": f"sh {h}"}]}]})
    p = spawn()
    p.boot()
    p.submit("x")
    lines = transcript_lines(env["CLAUDE_CONFIG_DIR"], work)
    res = [c for x in lines if x["type"] == "user" and isinstance(x["message"]["content"], list)
           for c in x["message"]["content"]]
    assert res[0]["is_error"] and "nope" in res[0]["content"]


def test_failing_tool_runs_post_tool_use_failure(spawn, env, tmp_path, scenario_file):
    out = tmp_path / "f.out"
    hook_settings(env["CLAUDE_CONFIG_DIR"], {"PostToolUseFailure": [{"matcher": "", "hooks": [
        {"type": "command", "command": f"cat >> {out}"}]}]})
    sc = scenario_file({"boot": {"bypass_dialog": False}, "turns": [{"match": ".*", "steps": [
        {"tool": "Bash", "input": {}, "result": "bad", "fail": True}]}]})
    p = spawn(FAKE_CLAUDE_SCENARIO=str(sc))
    p.wait_for("? for shortcuts")
    p.submit("x")
    pl = json.loads(out.read_text())
    assert pl["hook_event_name"] == "PostToolUseFailure" and pl["tool_response"]


def test_subagent_step(spawn, env, work, tmp_path, scenario_file):
    out = tmp_path / "sub.out"
    hook_settings(env["CLAUDE_CONFIG_DIR"], {ev: [{"matcher": "", "hooks": [
        {"type": "command", "command": f"cat >> {out}; echo >> {out}"}]}]
        for ev in ("SubagentStart", "SubagentStop")})
    sc = scenario_file({"boot": {"bypass_dialog": False}, "turns": [{"match": ".*", "steps": [
        {"subagent": {"id": "a1", "type": "Explore", "text": "found it"}}]}]})
    sid = "33333333-3333-4333-8333-333333333333"
    p = spawn(["--session-id", sid], FAKE_CLAUDE_SCENARIO=str(sc))
    p.wait_for("? for shortcuts")
    p.submit("x")
    proj = re.sub(r"[^A-Za-z0-9]", "-", work)
    f = Path(env["CLAUDE_CONFIG_DIR"]) / "projects" / proj / sid / "subagents" / "agent-a1.jsonl"
    rows = [json.loads(x) for x in f.read_text().splitlines()]
    assert rows and all(r["isSidechain"] is True for r in rows)
    pls = [json.loads(x) for x in out.read_text().splitlines()]
    assert [x["hook_event_name"] for x in pls] == ["SubagentStart", "SubagentStop"]
    assert pls[1]["agent_id"] == "a1" and pls[1]["agent_type"] == "Explore"
    assert pls[1]["agent_transcript_path"] == str(f)


def test_background_step_runs_stop_again(spawn, env, tmp_path, scenario_file):
    out = tmp_path / "bg.out"
    hook_settings(env["CLAUDE_CONFIG_DIR"], {"Stop": [{"matcher": "", "hooks": [
        {"type": "command", "command": f"cat >> {out}; echo >> {out}"}]}]})
    sc = scenario_file({"boot": {"bypass_dialog": False}, "turns": [{"match": ".*", "steps": [
        {"text": "first"}, {"background": {"after": 0.3, "text": "later"}}]}]})
    p = spawn(FAKE_CLAUDE_SCENARIO=str(sc))
    p.wait_for("? for shortcuts")
    p.submit("x")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        p.pump(0.1)
        if out.exists() and len(out.read_text().splitlines()) >= 2:
            break
    msgs = [json.loads(x)["last_assistant_message"] for x in out.read_text().splitlines()]
    assert msgs == ["first", "later"]


def test_hook_timeout_honoured(spawn, env, tmp_path, scenario_file):
    hook_settings(env["CLAUDE_CONFIG_DIR"], {"Stop": [{"matcher": "", "hooks": [
        {"type": "command", "command": "sleep 30", "timeout": 1}]}]})
    p = spawn()
    p.boot()
    t0 = time.monotonic()
    p.submit("x")
    assert time.monotonic() - t0 < 8


def test_plugin_flow(env, work, tmp_path):
    mkt = tmp_path / "mkt"
    (mkt / ".claude-plugin").mkdir(parents=True)
    (mkt / ".claude-plugin" / "marketplace.json").write_text(json.dumps({"name": "mymkt"}))
    r = run_cli(["plugin", "install", "p@mymkt", "-s", "user", "-y"], env, work)
    assert r.returncode == 1
    r = run_cli(["plugin", "marketplace", "add", str(tmp_path / "missing")], env, work)
    assert r.returncode == 1
    r = run_cli(["plugin", "marketplace", "add", str(mkt), "--json"], env, work)
    assert r.returncode == 0, r.stderr
    r = run_cli(["plugin", "marketplace", "list", "--json"], env, work)
    assert json.loads(r.stdout) == [{"name": "mymkt", "source": str(mkt), "path": str(mkt)}]
    r = run_cli(["plugin", "install", "p@mymkt", "-s", "user", "-y", "--json"], env, work)
    assert r.returncode == 0, r.stderr
    r = run_cli(["plugin", "list", "--json"], env, work)
    plugins = json.loads(r.stdout)
    assert plugins[0]["id"] == "p@mymkt" and plugins[0]["scope"] == "user"
    assert plugins[0]["enabled"] is True and plugins[0]["version"]
    assert (Path(env["CLAUDE_CONFIG_DIR"]) / "plugins" / "fake-state.json").exists()
    r = run_cli(["plugin", "install", "bad@mymkt"], {**env, "FAKE_CLAUDE_PLUGIN_FAIL": "bad@mymkt"},
                work)
    assert r.returncode == 1
    r = run_cli(["plugin", "marketplace", "add", "owner/repo"], env, work)
    assert r.returncode == 0
    names = [m["name"] for m in json.loads(
        run_cli(["plugin", "marketplace", "list", "--json"], env, work).stdout)]
    assert "repo" in names


def test_shim_fixture(fake_claude_bin):
    r = subprocess.run([str(fake_claude_bin), "--version"], capture_output=True, text=True,
                       check=False)
    assert r.stdout.strip() == "0.0.0 (Fake Claude)"
