"""Tatara example, turn path: finaliser, issue capture, outcome gate and statusline as hooks."""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import World, wait_turn

from claude_code_wrapper.interpolation import interpolate
from claude_code_wrapper.manager import CreateSessionRequest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "src/claude_code_wrapper/data/examples/tatara.config.yaml"
SCRIPT = ROOT / "src/claude_code_wrapper/data/examples/tatara/tatara_hooks.py"
SECRET = "hmac-secret"
OUTCOME = "mcp__tatara__submit_outcome"
ISSUE = "mcp__tatara__report_internal_issue"


class Receiver:
    """Loopback callback receiver: records (headers, raw body) of every POST."""

    def __init__(self) -> None:
        self.posts: list[tuple[dict[str, str], bytes]] = []
        posts = self.posts

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                posts.append(({k.lower(): v for k, v in self.headers.items()}, body))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a: Any) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/cb"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def bodies(self) -> list[dict]:
        return [json.loads(b) for _, b in self.posts]


@pytest.fixture
def receiver():
    r = Receiver()
    yield r
    r.server.shutdown()
    r.server.server_close()


@pytest.fixture
def env(monkeypatch, receiver) -> dict[str, str]:
    values = {
        "TATARA_PYTHON": sys.executable,
        "TATARA_HOOKS": str(SCRIPT),
        "TATARA_TASK": "task-7",
        "CALLBACK_HMAC_SECRET": SECRET,
        "DEFAULT_CALLBACK_URL": receiver.url,
    }
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    return values


def example(env: dict[str, str]) -> dict[str, Any]:
    return interpolate(yaml.safe_load(EXAMPLE.read_text()), env)


def sh(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def make_repo(root: Path, workdir: Path, name: str, remote: str | None = None) -> Path:
    """A clone of a fresh bare remote (or of `remote`) at workdir/name, with one change."""
    bare = root / f"{name}.git"
    if remote is None:
        sh("init", "--bare", "-b", "main", str(bare))
        remote = str(bare)
    repo = workdir / name
    repo.mkdir(parents=True)
    sh("init", "-b", "main", cwd=repo)
    sh("remote", "add", "origin", remote, cwd=repo)
    (repo / "a.txt").write_text("base\n")
    sh("add", "-A", cwd=repo)
    sh("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "base", cwd=repo)
    if remote == str(bare):
        sh("push", "origin", "main", cwd=repo)
    (repo / "a.txt").write_text("changed\n")
    return repo


async def start(world: World, env, scenario: dict, **extra: Any):
    ex = example(env)
    cfg = world.cfg(
        scenario, hooks=ex["hooks"], claude={"settings": ex["claude"]["settings"]}, **extra
    )
    m = world.manager(cfg)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    return m, sid, Path(m.get(sid)["workdir"])


def state_dir(world: World, sid: str) -> Path:
    return world.session_dir(sid) / "hook-state"


def turn(steps: list[dict], **kw: Any) -> dict:
    return {"turns": [{"match": "go", "steps": steps, **kw}]}


def tool(name: str, inp: dict, fail: bool = False) -> list[dict]:
    return [{"tool": name, "input": inp, "fail": fail}, {"sleep": 0.6}]


# ---------------------------------------------------------------- config


def test_example_hooks_shape(env):
    ex = example(env)
    hooks = ex["hooks"]
    assert hooks["onTurnComplete"][0]["blocking"] is False
    assert hooks["onToolResult"][0]["blocking"] is False
    assert hooks["onAgentToolCall"][0]["blocking"] is True
    assert hooks["onAgentToolCall"][0]["matcher"] == ISSUE
    assert hooks["onToolResult"][0]["matcher"] == OUTCOME
    assert hooks["onStop"][0]["blocking"] is True
    assert ex["claude"]["settings"]["statusLine"]["command"].endswith("statusline")


def test_script_is_executable_stdlib_with_header():
    text = SCRIPT.read_text()
    assert text.startswith("#!/usr/bin/env python3")
    assert SCRIPT.stat().st_mode & 0o111


# ---------------------------------------------------------------- finaliser


async def test_finalize_pushes_repos_drains_issues_and_signs_callback(
    world: World, env, receiver, tmp_path
):
    scenario = turn(
        [
            *tool(ISSUE, {"category": "tool_failure", "severity": "high", "summary": "boom"}),
            *tool(ISSUE, {"category": "weird", "severity": "urgent", "summary": "odd"}),
            {"text": "all done"},
        ]
    )
    m, sid, workdir = await start(world, env, scenario)
    good = make_repo(tmp_path, workdir, "good")
    bad = make_repo(tmp_path, workdir, "bad", remote=str(tmp_path / "missing.git"))
    clean = workdir / "clean"
    clean.mkdir()
    sh("init", "-b", "main", cwd=clean)
    # a statusline run inside claude persists the account limits
    cfgdir = next(world.session_dir(sid).rglob("settings.json")).parent
    command = example(env)["claude"]["settings"]["statusLine"]["command"]
    out = subprocess.run(
        command,
        shell=True,
        input=json.dumps({"model": {"display_name": "Fake"}, "rate_limits": {"five_hour": 12}}),
        env={"CLAUDE_CONFIG_DIR": str(cfgdir), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.strip() and "\n" not in out.strip()
    assert json.loads((cfgdir / "rate_limits.json").read_text()) == {"five_hour": 12}

    record = await m.router(sid).submit_turn("go")
    await wait_turn(m, sid)
    await world.until(lambda: receiver.posts, what="callback")
    headers, raw = receiver.posts[0]
    expected = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    assert headers["x-tatara-signature"] == expected
    body = json.loads(raw)
    assert body["turnId"] == record.turn_id
    assert body["taskName"] == "task-7"
    assert body["state"] == "complete"
    assert body["finalText"] == "all done"
    assert body["error"] is None
    assert body["durationSeconds"] >= 1.0
    assert set(body["usage"]) >= {"input_tokens", "output_tokens"}
    assert body["accountUsage"] == {"five_hour": 12}
    assert body["pushedRepos"] == ["good"]
    assert [f["repo"] for f in body["failedRepos"]] == ["bad"]
    assert body["failedRepos"][0]["error"]
    assert body["internalIssues"] == [
        {"category": "tool_failure", "severity": "high", "summary": "boom"},
        {"category": "other", "severity": "medium", "summary": "odd"},
    ]
    # the change reached the remote; the clean repo was left alone; issues were drained
    assert sh("log", "-1", "--format=%s", "main", cwd=tmp_path / "good.git")
    assert sh("show", "main:a.txt", cwd=tmp_path / "good.git") == "changed"
    assert sh("status", "--porcelain", cwd=good) == ""
    assert sh("status", "--porcelain", cwd=bad) == ""
    assert not (state_dir(world, sid) / "issues.jsonl").exists()
    assert len(receiver.posts) == 1


async def test_finalize_omits_account_usage_and_maps_failed_state(
    world: World, env, receiver, tmp_path
):
    scenario = {"turns": [{"match": "go", "steps": [{"text": "x"}]}]}
    m, sid, workdir = await start(world, env, scenario)
    envelope = {
        "event": "onTurnComplete",
        "turn_id": "t1",
        "workdir": str(workdir),
        "data": {"turn_id": "t1", "state": "timeout", "error": "boom", "usage": {}},
    }
    run = subprocess.run(
        [sys.executable, str(SCRIPT), "finalize"],
        input=json.dumps(envelope),
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "CCW_STATE_DIR": str(state_dir(world, sid)),
            "CCW_CLAUDE_CONFIG_DIR": str(tmp_path / "nocfg"),
            "TATARA_TASK": "task-7",
            "CALLBACK_HMAC_SECRET": SECRET,
            "DEFAULT_CALLBACK_URL": receiver.url,
        },
    )
    assert run.returncode == 0, run.stderr
    (body,) = receiver.bodies()
    assert body["state"] == "failed" and body["error"] == "boom"
    assert "accountUsage" not in body
    assert body["pushedRepos"] == [] and body["failedRepos"] == []


# ---------------------------------------------------------------- outcome gate


def outcome_steps(*, fail: bool) -> list[dict]:
    return tool(OUTCOME, {"status": "done"}, fail=fail)


async def test_gate_reprompts_until_cap_then_finalizes_once(world: World, env, receiver):
    scenario = turn(outcome_steps(fail=True), on_block=outcome_steps(fail=True))
    m, sid, _ = await start(world, env, scenario)
    record = await m.router(sid).submit_turn("go")
    await wait_turn(m, sid)
    await world.until(lambda: receiver.posts, what="callback")
    conts = [e["data"] for e in world.events(sid) if e["type"] == "turn_continued"]
    assert len(conts) == 2  # MAX_OUTCOME_REPROMPTS default
    assert all(c["turn_id"] == record.turn_id and c["reason"] for c in conts)
    assert len(receiver.posts) == 1  # nothing posted while a re-prompt was pending


async def test_gate_allows_after_accepted_outcome(world: World, env, receiver):
    scenario = turn(
        outcome_steps(fail=True),
        on_block=outcome_steps(fail=False),
    )
    m, sid, _ = await start(world, env, scenario)
    await m.router(sid).submit_turn("go")
    await wait_turn(m, sid)
    await world.until(lambda: receiver.posts, what="callback")
    conts = [e for e in world.events(sid) if e["type"] == "turn_continued"]
    assert len(conts) == 1
    outcome = json.loads((state_dir(world, sid) / "outcome.json").read_text())
    assert outcome["is_error"] is False


async def test_gate_allows_without_outcome_and_respects_env_cap(
    world: World, env, receiver, monkeypatch
):
    monkeypatch.setenv("MAX_OUTCOME_REPROMPTS", "1")
    scenario = turn(outcome_steps(fail=True), on_block=outcome_steps(fail=True))
    m, sid, _ = await start(world, env, scenario)
    await m.router(sid).submit_turn("go")
    await wait_turn(m, sid)
    assert len([e for e in world.events(sid) if e["type"] == "turn_continued"]) == 1

    m2, sid2, _ = await start(world, env, {"turns": [{"match": "go", "steps": [{"text": "q"}]}]})
    await m2.router(sid2).submit_turn("go")
    await wait_turn(m2, sid2)
    assert not [e for e in world.events(sid2) if e["type"] == "turn_continued"]


def run_script(sub: str, stdin: dict, state: Path, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), sub],
        input=json.dumps(stdin),
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "CCW_STATE_DIR": str(state), **env},
    )


def test_gate_prints_block_decision_and_counts(tmp_path):
    state = tmp_path / "st"
    state.mkdir()
    (state / "outcome.json").write_text(json.dumps({"turn_id": "t1", "is_error": True}))
    run = run_script("outcome-gate", {"turn_id": "t1"}, state, MAX_OUTCOME_REPROMPTS="2")
    out = json.loads(run.stdout)
    assert out["decision"] == "block" and out["reason"]
    assert (state / "reprompts-t1").read_text().strip() == "1"
    # another turn's rejection does not gate this turn
    assert run_script("outcome-gate", {"turn_id": "t2"}, state).stdout.strip() == ""
    # the cap never exceeds claude's own (8)
    (state / "reprompts-t1").write_text("6")
    assert run_script("outcome-gate", {"turn_id": "t1"}, state, MAX_OUTCOME_REPROMPTS="50").stdout
    assert not run_script(
        "outcome-gate", {"turn_id": "t1"}, state, MAX_OUTCOME_REPROMPTS="50"
    ).stdout


def test_capture_issue_clamps_and_allows(tmp_path):
    state = tmp_path / "st"
    state.mkdir()
    run = run_script(
        "capture-issue",
        {"data": {"tool_input": {"category": "nope", "severity": "low", "summary": "s"}}},
        state,
    )
    assert json.loads(run.stdout)["decision"] == "allow"
    (line,) = (state / "issues.jsonl").read_text().splitlines()
    assert json.loads(line) == {"category": "other", "severity": "low", "summary": "s"}


def test_statusline_without_rate_limits_writes_nothing(tmp_path):
    run = run_script(
        "statusline", {"model": {"display_name": "M"}}, tmp_path, CLAUDE_CONFIG_DIR=str(tmp_path)
    )
    assert run.returncode == 0 and run.stdout.strip()
    assert not (tmp_path / "rate_limits.json").exists()


def test_finalize_posts_nothing_while_reprompt_pending(tmp_path, receiver):
    state = tmp_path / "st"
    state.mkdir()
    (state / "outcome.json").write_text(json.dumps({"turn_id": "t1", "is_error": True}))
    env = {
        "CCW_STATE_DIR": str(state),
        "DEFAULT_CALLBACK_URL": receiver.url,
        "CALLBACK_HMAC_SECRET": SECRET,
        "CCW_WORKDIR": str(tmp_path),
    }
    envelope = {"turn_id": "t1", "data": {"turn_id": "t1", "state": "completed"}}
    assert run_script("finalize", envelope, state, **env).returncode == 0
    assert receiver.posts == []
    (state / "reprompts-t1").write_text("2")  # cap reached: the gate lets the turn end
    assert run_script("finalize", envelope, state, **env).returncode == 0
    assert len(receiver.posts) == 1
